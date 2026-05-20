from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict


SignalType = Literal["int", "float", "str", "list[str]", "bool"]


class ProfileSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    type: SignalType
    description: str


class GameProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    taxonomy: list[str] = Field(default_factory=list)
    signals: list[ProfileSignal] = Field(default_factory=list)
    prompt_hints: str = ""

    def signal_keys(self) -> list[str]:
        return [s.key for s in self.signals]


GTA_PROFILE = GameProfile(
    name="gta",
    taxonomy=[
        "police_chase", "shootout", "stunt", "crash",
        "npc_chaos", "cruising", "mission", "fail",
    ],
    signals=[
        ProfileSignal(
            key="wanted_level", type="int",
            description="On-screen wanted stars, 0-5; null if not visible.",
        ),
        ProfileSignal(
            key="vehicles", type="list[str]",
            description="Vehicle types visible in the segment.",
        ),
        ProfileSignal(
            key="events", type="list[str]",
            description="Recognized on-screen event text: WASTED, BUSTED, MISSION PASSED, MISSION FAILED.",
        ),
        ProfileSignal(
            key="location", type="str",
            description="In-game location or environment.",
        ),
        ProfileSignal(
            key="npcs", type="str",
            description="Notable NPC presence or interactions.",
        ),
    ],
    prompt_hints=(
        "This is Grand Theft Auto gameplay. Pay attention to the wanted-level stars "
        "(top-right HUD), vehicles, pedestrians, and on-screen mission/status text."
    ),
)


BUILTINS: dict[str, GameProfile] = {
    "gta": GTA_PROFILE,
}


def load_profile(name_or_path: str) -> GameProfile:
    if name_or_path in BUILTINS:
        return BUILTINS[name_or_path]
    p = Path(name_or_path)
    if not p.exists():
        raise ValueError(
            f"Unknown game profile '{name_or_path}'. "
            f"Built-ins: {sorted(BUILTINS)}; or pass a path to a JSON file."
        )
    with p.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return GameProfile.model_validate(data)
