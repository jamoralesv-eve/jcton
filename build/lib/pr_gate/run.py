import uuid

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from pr_gate.agent import root_agent
from pr_gate.decide import as_verdict
from pr_gate.errors import GateError

APP_NAME = "pr_gate"
USER_ID = "cli"
TRIGGER = "Evalúa este PR"


async def evaluate(diff: str, ticket: str) -> dict:
    service = InMemorySessionService()
    session_id = str(uuid.uuid4())
    state = {"pr_diff": diff, "original_ticket": ticket}
    await _open_session(service, session_id, state)
    await _run(service, session_id)
    return await _read_verdict(service, session_id)


async def _open_session(
    service: InMemorySessionService,
    session_id: str,
    state: dict,
) -> None:
    await service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
        state=state,
    )


async def _run(service: InMemorySessionService, session_id: str) -> None:
    runner = Runner(agent=root_agent, app_name=APP_NAME, session_service=service)
    message = types.Content(role="user", parts=[types.Part(text=TRIGGER)])
    async for _event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=message,
    ):
        pass


async def _read_verdict(service: InMemorySessionService, session_id: str) -> dict:
    session = await service.get_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=session_id,
    )
    if session is None:
        raise GateError("La sesión del gate no existe")
    if "verdict" not in session.state:
        raise GateError("El comparador no escribió un veredicto")
    return as_verdict(session.state["verdict"])
