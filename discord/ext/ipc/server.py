import logging
import aiohttp.web

from aiohttp.web import Application, TCPSite, AppRunner, Request
from discord.ext.ipc.errors import *
from discord.ext.commands import Bot

log = logging.getLogger(__name__)

def route(name=None):
    """
    |method|
    Used to register a coroutine as an endpoint when you don't have
    access to an instance of :class:`.Server`

    Parameters
    ----------
    name: :str:`str`
        The endpoint name. If not provided the method name will be
        used.
    """
    def decorator(func):
        Server.ROUTES[name or func.__name__] = func
        return func
    return decorator

class IpcServerResponse:
    def __init__(self, data):
        self._json = data
        self.length = len(data)

        self.endpoint = data["endpoint"]

        for key, value in data["data"].items():
            setattr(self, key, value)

    def to_json(self):
        return self._json

    def __repr__(self):
        return "<IpcServerResponse length={0.length}>".format(self)

    def __str__(self):
        return self.__repr__()

class Server:
    """ 
    |class|

    The IPC server. Usually used on the bot process for receiving
    requests from the client.

    Attributes
    ----------
    bot: :class:`~discord.ext.commands.Bot`
        Your bot instance
    host: :str:`str`
        The host to run the IPC Server on. Defaults to `127.0.0.1`.
    port: :str:`int`
        The port to run the IPC Server on. Defaults to 1010.
    secret_key: :str:`str`
        A secret key. Used for authentication and should be the same as
        your client's secret key.
    do_multicast: :bool:`bool`
        Turn multicasting on/off. Defaults to True
    multicast_port: :int:`int`
        The port to run the multicasting server on. Defaults to 20000
    """

    ROUTES = {}

    def __init__(
        self, 
        bot: Bot, 
        host: str = "127.0.0.1", 
        port: int = 1010, 
        secret_key: str = None, 
        do_multicast: bool = True,
        multicast_port: int = 20000,
        logger: logging.Logger = log
    ):
        self.bot = bot
        self.host = host
        self.port = port
        self.secret_key = secret_key
        self.do_multicast = do_multicast
        self.multicast_port = multicast_port
        self.logger = logger
        self._server = None
        self._multicast_server = None
        self.endpoints = {}

    def start(self) -> None:
        """
        |method|
        
        Starts the IPC server
        """
        self._server = Application()
        self._server.router.add_route("GET", "/", self.handle_accept)

        if self.do_multicast:
            self._multicast_server = Application()
            self._multicast_server.router.add_route("GET", "/", self.handle_multicast)
            self.bot.loop.create_task(self.setup(self._multicast_server, self.multicast_port))
        
        self.bot.loop.create_task(self.setup(self._server, self.port))
        self.bot.dispatch("ipc_ready")
        self.logger.info("The IPC server is ready")

    def route(self, name: str = None):
        """
        |method|

        Used to register a coroutine as an endpoint when you have
        access to an instance of :class:`~discord.ext.ipc.Server`
        
        Please note that the endpoints are registered in :function:`update_endpoints`

        Parameters
        ----------
        name: `str`
            The endpoint name. If not provided the method name will be used.
        """
        def decorator(func):
            if not name:
                self.endpoints[func.__name__] = func
            else:
                self.endpoints[name] = func
            return func
        return decorator

    def update_endpoints(self) -> None:
        """
        |method|
        
        Called internally to update the server's endpoints for cog routes.
        """
        self.endpoints = {**self.endpoints, **self.ROUTES}
        self.ROUTES = {}

    async def handle_accept(self, request: Request) -> None:
        """
        |coro|

        Handles websocket requests from the client process.

        Parameters
        ----------
        request: :class:`~aiohttp.web.Request`
            The request made by the client, parsed by aiohttp.
        """
        self.update_endpoints() # VERRY IMPORTANT !!!!!!
        self.logger.info("Handing new IPC request")

        websocket = aiohttp.web.WebSocketResponse()
        await websocket.prepare(request)

        async for message in websocket:
            request = message.json()

            self.logger.debug("IPC Server < %r", request)

            endpoint = request.get("endpoint")
            headers = request.get("headers")

            if not headers or headers.get("Authorization") != self.secret_key:
                self.bot.dispatch("ipc_error", endpoint, Exception("Received unauthorized request (Invalid or no token provided)."))
                response = {
                    "error": "Received unauthorized request (invalid or no token provided).", 
                    "code": 403
                }
            else:
                if not endpoint or endpoint not in self.endpoints:
                    self.bot.dispatch("ipc_error", endpoint, Exception("Received invalid request (invalid or no endpoint given)."))
                    response = {
                        "error": "Received invalid request (invalid or no endpoint given).",
                        "code": 400
                    }
                else:
                    server_response = IpcServerResponse(request)
                    
                    try:
                        attempted_cls = self.bot.cogs.get(
                            self.endpoints[endpoint].__qualname__.split(".")[0]
                        )

                        if attempted_cls:
                            arguments = (attempted_cls, server_response)
                        else:
                            arguments = (server_response,)
                    except AttributeError:
                        arguments = (server_response,)

                    try:
                        ret = await self.endpoints[endpoint](*arguments)
                        response = ret
                    except Exception as error:
                        self.logger.error(
                            "Received error while executing %r with %r",
                            endpoint,
                            request,
                        )
                        self.bot.dispatch("ipc_error", endpoint, error)

                        response = {
                            "error": str(error),
                            "code": 500,
                        }

            try:
                if not response.get("code"):
                    response["code"] = 200
                await websocket.send_json(response)
                self.logger.debug("IPC Server > %r", response)
            except TypeError as error:
                if str(error).startswith("Object of type") and str(error).endswith("is not JSON serializable"):
                    error_response = (
                        "IPC route returned values which are not able to be sent over sockets."
                        "If you are trying to send a discord.py object,"
                        "please only send the data you need."
                    )

                    self.bot.dispatch("ipc_error", endpoint, Exception(error_response))

                    response = {
                        "error": error_response, 
                        "code": 500
                    }

                    await websocket.send_json(response)
                    self.logger.debug("IPC Server > %r", response)

                    raise JSONEncodeError(error_response)

    async def handle_multicast(self, request: Request) -> None:
        """
        |coro|

        Handles multicasting websocket requests from the client.

        Parameters
        ----------
        request: :class:`~aiohttp.web.Request`
            The request made by the client, parsed by aiohttp.
        """
        self.logger.info("Initiating Multicast Server.")
        websocket = aiohttp.web.WebSocketResponse()
        await websocket.prepare(request)

        async for message in websocket:
            request = message.json()

            log.debug("Multicast Server < %r", request)

            headers = request.get("headers")

            if not headers or headers.get("Authorization") != self.secret_key:
                response = {"error": "Invalid or no token provided.", "code": 403}
            else:
                response = {
                    "message": "Connection success",
                    "port": self.port,
                    "code": 200,
                }

            self.logger.debug("Multicast Server > %r", response)
            await websocket.send_json(response)

    async def setup(self, application: Application, port: int) -> None:
        """
        |coro|

        This function stats the IPC runner and the IPC webserver

        Parameters
        ----------
        application: :class:`aiohttp.web.Application`
            The internal router's app
        port: :int:`int`
            The specific port to run the application (:class:`~aiohttp.web.Application`)
        """
        self.logger.debug('Starting the IPC runner')
        self._runner = _runner = AppRunner(application)
        await _runner.setup()

        self.logger.debug('Starting the IPC webserver')
        self._webserver = _webserver = TCPSite(_runner, self.host, port)
        await _webserver.start()

    async def destroy(self) -> None:
        """
        |coro|

        Stops both the IPC runner and the IPC webserver
        """
        self.logger.debug('Cleaning up the IPC runner')
        await self._runner.cleanup()
        if self._webserver:
            self.logger.debug('Closing the IPC webserver')
            await self._webserver.stop()
