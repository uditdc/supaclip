from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SignalType = Literal["int", "float", "str", "list[str]", "bool"]


class ProfileSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    type: SignalType
    description: str


class Character(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    images: list[str] = Field(default_factory=list)
    description: str = ""

    @model_validator(mode="after")
    def _resolve_images(self) -> "Character":
        resolved: list[str] = []
        for raw in self.images:
            p = Path(raw).expanduser()
            if not p.is_file():
                raise ValueError(f"character image not found: {raw}")
            resolved.append(str(p.resolve()))
        self.images = resolved
        return self


class VideoContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intro: str = ""
    characters: list[Character] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.intro.strip() and not self.characters

    @classmethod
    def load(cls, path: str | Path) -> "VideoContext":
        p = Path(path).expanduser()
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls.model_validate(data)


DEFAULT_BOUNDARY_RULES = (
    "A new event begins whenever the on-screen subject, location, or activity\n"
    "changes meaningfully — track the dominant action, setting, and any on-screen\n"
    "text. Windows must not overlap."
)


class GameProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    subject: str = "video"
    taxonomy: list[str] = Field(default_factory=list)
    signals: list[ProfileSignal] = Field(default_factory=list)
    prompt_hints: str = ""
    boundary_rules: str = ""
    example_json: str = ""
    prompt_template_video: str | None = None
    prompt_template_frames: str | None = None

    def signal_keys(self) -> list[str]:
        return [s.key for s in self.signals]

    def effective_boundary_rules(self) -> str:
        return self.boundary_rules.strip() or DEFAULT_BOUNDARY_RULES


GTA_PROFILE = GameProfile(
    name="gta",
    subject="Grand Theft Auto gameplay",
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
    boundary_rules=(
        "A new situation begins when ANY of these change: on-foot vs in-vehicle\n"
        "state, vehicle being driven, wanted level, activity (walking, driving,\n"
        "shooting, crashing), on-screen mission/event text, or location/environment."
    ),
    example_json=(
        'EXAMPLE for a 90-second clip where the player walks to a parked car,\n'
        'steals it, and gets chased by police — return THREE events, NOT one:\n'
        '{\n'
        '  "events": [\n'
        '    {"start": 0.0, "end": 18.0, "description": "Player walks down a city sidewalk past pedestrians toward a parked sedan.", "categories": ["cruising"], "base_interest": 20, "game_signals": {"wanted_level": 0, "vehicles": [], "location": "Vinewood Boulevard"}},\n'
        '    {"start": 18.0, "end": 38.0, "description": "Player pulls the driver out of a sedan and accelerates away; two stars appear in the HUD.", "categories": ["npc_chaos"], "base_interest": 55, "game_signals": {"wanted_level": 2, "vehicles": ["sedan"], "events": [], "location": "Vinewood Boulevard"}},\n'
        '    {"start": 38.0, "end": 90.0, "description": "High-speed chase on the freeway with two police cruisers in pursuit.", "categories": ["police_chase"], "base_interest": 85, "game_signals": {"wanted_level": 3, "vehicles": ["sedan", "police_cruiser"], "events": [], "location": "Los Santos freeway"}}\n'
        '  ]\n'
        '}'
    ),
)


GAME_TRAILER_PROFILE = GameProfile(
    name="game_trailer",
    subject="video game trailer",
    taxonomy=[
        "logo_reveal", "cinematic", "gameplay", "combat",
        "boss_reveal", "character_reveal", "world_reveal",
        "cutscene", "title_card", "release_date", "credits",
    ],
    signals=[
        ProfileSignal(
            key="shot_type", type="str",
            description="Cinematic vs in-engine gameplay vs UI/title card.",
        ),
        ProfileSignal(
            key="on_screen_text", type="list[str]",
            description="Title cards, taglines, studio logos, release dates, platform badges.",
        ),
        ProfileSignal(
            key="characters", type="list[str]",
            description="Recognizable characters, heroes, or villains visible.",
        ),
        ProfileSignal(
            key="setting", type="str",
            description="Environment, world, or biome shown.",
        ),
        ProfileSignal(
            key="action_intensity", type="str",
            description="Low / medium / high based on motion, cuts, and combat.",
        ),
        ProfileSignal(
            key="music_cue", type="str",
            description="Music mood or beat drop if discernible from audio.",
        ),
    ],
    prompt_hints=(
        "This is a video game trailer. Distinguish cinematic/pre-rendered shots from "
        "in-engine gameplay. Track title cards, logos, release dates, and platform badges. "
        "Note hero/villain reveals and major beat drops."
    ),
    boundary_rules=(
        "A new beat begins when ANY of these change: shot mode (cinematic vs\n"
        "gameplay vs title card), the focal character or enemy, the world/setting,\n"
        "the music phrase (beat drop, drum hit, silence), or major on-screen text\n"
        "(logo, tagline, release date)."
    ),
)


MOVIE_TRAILER_PROFILE = GameProfile(
    name="movie_trailer",
    subject="movie trailer",
    taxonomy=[
        "studio_logo", "cold_open", "dialogue", "action",
        "montage", "title_card", "needle_drop", "release_date",
        "cast_card", "credits_block",
    ],
    signals=[
        ProfileSignal(
            key="shot_type", type="str",
            description="Wide / close-up / dialogue / action / title card.",
        ),
        ProfileSignal(
            key="on_screen_text", type="list[str]",
            description="Title cards, taglines, studio names, release dates, cast names.",
        ),
        ProfileSignal(
            key="characters", type="list[str]",
            description="Named or recognizable on-screen characters.",
        ),
        ProfileSignal(
            key="setting", type="str",
            description="Location, era, or environment shown.",
        ),
        ProfileSignal(
            key="mood", type="str",
            description="Tonal mood: tense, comedic, romantic, somber, epic, etc.",
        ),
        ProfileSignal(
            key="dialogue_snippet", type="str",
            description="Short quoted dialogue if clearly audible.",
        ),
    ],
    prompt_hints=(
        "This is a movie trailer. Watch for studio logos, title cards, taglines, "
        "release dates, and cast cards. Identify the three-act trailer structure "
        "(setup, escalation, button) and mark needle-drops or tonal shifts."
    ),
    boundary_rules=(
        "A new beat begins when ANY of these change: trailer act (setup → escalation\n"
        "→ button), the tonal register (quiet/loud, comedic/serious), the focal\n"
        "character, the location/era, or major on-screen text (studio logo, title\n"
        "card, release date, cast card)."
    ),
)


MOVIE_PROFILE = GameProfile(
    name="movie",
    subject="film scene",
    taxonomy=[
        "establishing", "dialogue", "action", "chase",
        "fight", "romance", "montage", "flashback",
        "comedy_beat", "emotional_beat", "credits",
    ],
    signals=[
        ProfileSignal(
            key="shot_type", type="str",
            description="Establishing / wide / medium / close-up / insert / POV.",
        ),
        ProfileSignal(
            key="characters", type="list[str]",
            description="Characters present in the scene.",
        ),
        ProfileSignal(
            key="setting", type="str",
            description="Location, time of day, era.",
        ),
        ProfileSignal(
            key="dialogue_snippet", type="str",
            description="Short quoted dialogue if clearly audible.",
        ),
        ProfileSignal(
            key="mood", type="str",
            description="Tonal mood of the scene.",
        ),
        ProfileSignal(
            key="key_action", type="str",
            description="Main action or beat driving the scene.",
        ),
    ],
    prompt_hints=(
        "This is a full-length film or scene. Track characters, locations, and the "
        "main beat of each scene. Distinguish dialogue scenes from action and montages."
    ),
    boundary_rules=(
        "A new scene begins when ANY of these change: location, time of day, the\n"
        "set of present characters, or the dramatic beat (dialogue → action,\n"
        "tension → release). Treat hard cuts to a new location or character group\n"
        "as a definite boundary."
    ),
)


SPORTS_PROFILE = GameProfile(
    name="sports",
    subject="sports broadcast",
    taxonomy=[
        "play", "goal", "score", "foul", "replay",
        "celebration", "commentary", "crowd", "highlight",
    ],
    signals=[
        ProfileSignal(
            key="sport", type="str",
            description="Sport being played (soccer, basketball, cricket, etc.).",
        ),
        ProfileSignal(
            key="teams", type="list[str]",
            description="Team names or jersey colors visible.",
        ),
        ProfileSignal(
            key="scoreboard", type="str",
            description="Score, clock, or period shown on the HUD.",
        ),
        ProfileSignal(
            key="key_event", type="str",
            description="Goal, shot, tackle, dunk, wicket, or other notable play.",
        ),
        ProfileSignal(
            key="players", type="list[str]",
            description="Player names or numbers visible.",
        ),
        ProfileSignal(
            key="is_replay", type="bool",
            description="Whether the segment is a replay rather than live action.",
        ),
    ],
    prompt_hints=(
        "This is sports footage. Read the scoreboard/HUD for score, clock, and period. "
        "Mark goals, scores, fouls, and celebrations. Flag replays separately from live play."
    ),
    boundary_rules=(
        "A new event begins when ANY of these change: live play vs replay, the\n"
        "scoreboard (score/clock/period), the phase of play (build-up, scoring\n"
        "attempt, celebration, stoppage), or the camera mode (live broadcast →\n"
        "commentary booth → crowd shot)."
    ),
)


PODCAST_PROFILE = GameProfile(
    name="podcast",
    subject="podcast or interview",
    taxonomy=[
        "intro", "monologue", "interview", "debate",
        "joke", "story", "ad_read", "outro",
    ],
    signals=[
        ProfileSignal(
            key="speakers", type="list[str]",
            description="Visible or named speakers in the segment.",
        ),
        ProfileSignal(
            key="topic", type="str",
            description="Topic or subject being discussed.",
        ),
        ProfileSignal(
            key="quote", type="str",
            description="A short quotable line if clearly delivered.",
        ),
        ProfileSignal(
            key="mood", type="str",
            description="Tonal mood: serious, comedic, heated, reflective.",
        ),
        ProfileSignal(
            key="is_ad", type="bool",
            description="Whether the segment is a sponsor/ad read.",
        ),
    ],
    prompt_hints=(
        "This is a podcast or interview. Identify speakers, the topic in play, and "
        "punchy quotable moments. Flag ad reads and intros/outros separately."
    ),
    boundary_rules=(
        "A new segment begins when ANY of these change: the topic of discussion,\n"
        "the active speaker, the tonal register (serious ↔ comedic), or the mode\n"
        "of speech (monologue, dialogue, ad read, intro/outro music)."
    ),
)


VLOG_PROFILE = GameProfile(
    name="vlog",
    subject="vlog or lifestyle video",
    taxonomy=[
        "intro", "to_camera", "b_roll", "activity",
        "reaction", "food", "travel", "outro",
    ],
    signals=[
        ProfileSignal(
            key="creator_visible", type="bool",
            description="Whether the creator is on camera in the segment.",
        ),
        ProfileSignal(
            key="location", type="str",
            description="Place or environment shown.",
        ),
        ProfileSignal(
            key="activity", type="str",
            description="What the creator is doing.",
        ),
        ProfileSignal(
            key="on_screen_text", type="list[str]",
            description="Captions, lower thirds, or graphics on screen.",
        ),
        ProfileSignal(
            key="mood", type="str",
            description="Tonal mood of the segment.",
        ),
    ],
    prompt_hints=(
        "This is a vlog or lifestyle video. Distinguish to-camera segments from b-roll. "
        "Note location, activity, and any on-screen captions or graphics."
    ),
    boundary_rules=(
        "A new segment begins when ANY of these change: to-camera vs b-roll mode,\n"
        "location, the activity the creator is doing, or major on-screen captions\n"
        "and lower thirds."
    ),
)


TUTORIAL_PROFILE = GameProfile(
    name="tutorial",
    subject="tutorial or instructional video",
    taxonomy=[
        "intro", "explanation", "demo", "code",
        "diagram", "step", "warning", "summary",
    ],
    signals=[
        ProfileSignal(
            key="topic", type="str",
            description="Subject of the tutorial.",
        ),
        ProfileSignal(
            key="visual_type", type="str",
            description="Talking head / screen recording / slide / diagram / code editor.",
        ),
        ProfileSignal(
            key="on_screen_text", type="list[str]",
            description="Headings, captions, code, or callouts visible.",
        ),
        ProfileSignal(
            key="step", type="str",
            description="Current step or section being covered.",
        ),
        ProfileSignal(
            key="key_takeaway", type="str",
            description="Main point or instruction in the segment.",
        ),
    ],
    prompt_hints=(
        "This is an instructional or tutorial video. Track the current step or section, "
        "the visual mode (slides, code, screen recording, talking head), and capture "
        "the key takeaway of each segment."
    ),
    boundary_rules=(
        "A new segment begins when ANY of these change: the step/section being\n"
        "taught, the visual mode (talking head ↔ slides ↔ screen recording ↔\n"
        "code editor ↔ diagram), or the topic/key takeaway."
    ),
)


BUILTINS: dict[str, GameProfile] = {
    "gta": GTA_PROFILE,
    "game_trailer": GAME_TRAILER_PROFILE,
    "movie_trailer": MOVIE_TRAILER_PROFILE,
    "movie": MOVIE_PROFILE,
    "sports": SPORTS_PROFILE,
    "podcast": PODCAST_PROFILE,
    "vlog": VLOG_PROFILE,
    "tutorial": TUTORIAL_PROFILE,
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
