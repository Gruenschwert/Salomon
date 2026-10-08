"""Attrappen für externe Dienste. In Tests findet kein echter API-Aufruf statt."""

from types import SimpleNamespace


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(name: str, eingabe: dict, block_id: str = "toolu_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=block_id, name=name, input=eingabe)


def claude_antwort(
    *bloecke: SimpleNamespace,
    stop_reason: str | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> SimpleNamespace:
    if stop_reason is None:
        hat_tool = any(block.type == "tool_use" for block in bloecke)
        stop_reason = "tool_use" if hat_tool else "end_turn"
    return SimpleNamespace(
        content=list(bloecke),
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class FakeAnthropic:
    """Liefert vorbereitete Antworten der Reihe nach; die letzte wird beliebig oft wiederholt."""

    def __init__(self, *antworten) -> None:
        self._antworten = list(antworten)
        self.aufrufe: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.aufrufe.append({**kwargs, "messages": list(kwargs["messages"])})
        antwort = self._antworten.pop(0) if len(self._antworten) > 1 else self._antworten[0]
        if isinstance(antwort, Exception):
            raise antwort
        return antwort


def system_text(aufruf: dict) -> str:
    """Der System-Prompt eines Aufrufs als ein Text (die API bekommt ihn in Blöcken)."""
    system = aufruf["system"]
    return system if isinstance(system, str) else "".join(block["text"] for block in system)
