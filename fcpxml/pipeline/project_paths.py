from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    base: Path          # project root for inputs
    workspace: Path     # workspace root for outputs

    @property
    def working_root(self) -> Path:
        return self.workspace

    @property
    def info_fcpxml(self) -> Path:
        return self.base / "Info.fcpxml"

    @property
    def master_manifest(self) -> Path:
        return self.working_root / "master_manifest.json"
    @property
    def scene_manifest_enriched(self) -> Path:
        return self.working_root / "scene_manifest_enriched.json"
    @property
    def scene_manifest(self) -> Path:
        return self.working_root / "scene_manifest_transcribed.json"

    @property
    def enriched_manifest(self) -> Path:
        return self.working_root / "15_enriched_manifest.json"

    @property
    def countdown_offsets(self) -> Path:
        return self.working_root / "20_countdown_offsets.json"

    @property
    def adjustment_map(self) -> Path:
        return self.working_root / "25_adjustment_map.json"

    @property
    def final_fcpxml(self) -> Path:
        return self.working_root / "ai_final_cut.fcpxml"

    @property
    def adjusted_fcpxml(self) -> Path:
        return self.working_root / "26_adjusted.fcpxml"

    # Directories
    @property
    def dehydrated_tpl(self) -> Path:
        return self.working_root / "15_dehydrated_template"

    @property
    def rehydrated_tpl(self) -> Path:
        return self.working_root / "15_rehydrated_template"

    @property
    def ignored_clips(self) -> Path:
        return self.dehydrated_tpl / "ignored_clips"

    @property
    def process_clips(self) -> Path:
        return self.dehydrated_tpl / "process_clips"

    @property
    def mc_clips(self) -> Path:
        return self.rehydrated_tpl / "mc_clips"

    @property
    def proxies(self) -> Path:
        return self.base / "proxies"

    @property
    def temp_audio(self) -> Path:
        return self.working_root / "temp_audio"

    @property
    def plans_dir(self) -> Path:
        return self.working_root / "45_ai_pacing_plans"

    @property
    def b_roll_dir(self) -> Path: # <-- ADD THIS PROPERTY
        return self.working_root / "46_ai_b_roll_plans"

    @property
    def ai_ignore(self) -> Path:
        return self.base / ".ai_ignore"

    # Template resource parts
    @property
    def header_xml(self) -> Path:
        return self.dehydrated_tpl / "00_header.xml"

    @property
    def resources_xml(self) -> Path:
        return self.dehydrated_tpl / "01_resources.xml"

    @property
    def library_open_xml(self) -> Path:
        return self.dehydrated_tpl / "02_library_open.xml"

    @property
    def footer_xml(self) -> Path:
        return self.dehydrated_tpl / "99_footer.xml"


# ---- Project config registry (projects/ directory) ----
CONFIG_DIR = Path(__file__).parent / "projects"
ACTIVE_FILE = CONFIG_DIR / "active_project.txt"


def _ensure_config_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def list_projects() -> list[str]:
    _ensure_config_dir()
    return [p.stem for p in CONFIG_DIR.glob("*.json")]


def load_project_config(name: str) -> dict | None:
    path = CONFIG_DIR / f"{name}.json"
    if not path.exists():
        return None
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def save_project_config(name: str, config: dict) -> Path:
    _ensure_config_dir()
    import json
    path = CONFIG_DIR / f"{name}.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def get_active_project_name() -> str | None:
    if not ACTIVE_FILE.exists():
        return None
    return ACTIVE_FILE.read_text(encoding="utf-8").strip() or None


def set_active_project_name(name: str):
    _ensure_config_dir()
    ACTIVE_FILE.write_text(name, encoding="utf-8")


def load_active_project_config() -> dict | None:
    name = get_active_project_name()
    if not name:
        return None
    return load_project_config(name)


def add_base_arg(parser):
    parser.add_argument(
        "--base",
        type=Path,
        default=None,
        help="Project base directory; if omitted, uses active project config.",
    )
    parser.add_argument(
        "--workspace-subdir",
        type=str,
        default=None,
        help="Optional subdirectory under base for all generated files (e.g., ai_working_files).",
    )
    return parser
def get_projects_root() -> str:
    """
    Finds the root directory that contains all projects.
    It does this by loading the active project and getting its parent directory.
    e.g., if active project base is 'E:/davinci/projects/my_project', this returns
    'E:/davinci/projects'.
    """
    cfg = load_active_project_config()
    if not cfg or "base_project_directory" not in cfg:
        # Fallback if no config is found
        return str(Path.cwd())
        
    active_project_path = Path(cfg["base_project_directory"])
    # The "projects root" is the parent of any individual project's base directory
    return str(active_project_path.parent)


def from_args(args) -> ProjectPaths:
    # Prefer explicit args when provided
    base: Path | None = getattr(args, "base", None)
    subdir = getattr(args, "workspace_subdir", None)

    if base is None:
        # Try active project config
        cfg = load_active_project_config()
        if cfg:
            base = Path(cfg.get("base_project_directory", cfg.get("base", Path.cwd())))
            subdir = cfg.get("workspace_subdir", subdir)
    if base is None:
        base = Path.cwd()

    workspace = base / subdir if subdir else base
    return ProjectPaths(base=base, workspace=workspace)