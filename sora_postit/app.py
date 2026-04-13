from __future__ import annotations

import csv
import json
import re
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

import typer
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from rich.console import Console


app = typer.Typer(
    add_completion=False,
    help="Overwrite Sora draft prompt text with numbered names and optionally post through a real browser session.",
)
console = Console()

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE_URL = "https://sora.chatgpt.com"
TILE_SELECTOR = "a[href^='/d/']"
CAPTION_SELECTOR = "textarea[placeholder='Add caption...']"
POST_TIMEOUT_MS = 45_000
GENERAL_TIMEOUT_MS = 15_000
NUMBERED_TITLE_RE = re.compile(r"\bdraft_(\d{1,7})\b", re.IGNORECASE)
SKIPPABLE_POST_PATTERNS = (
    "warning",
    "error",
    "does not exist",
    "doesn't exist",
    "tagged",
    "character",
    "mentioned a user",
    "does not have a cameo",
    "doesn't have a cameo",
    "no cameo",
    "cameo",
    "not found",
    "unable to post",
    "cannot post",
)


class DraftsDepletedError(RuntimeError):
    """Raised when no remaining unprocessed draft tile can be found."""


def now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def make_slug() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")


def make_title(number: int) -> str:
    return f"draft_{number:06d}"


def ensure_absolute(path_value: Path) -> Path:
    return path_value.expanduser().resolve()


def serialize_path(path_value: Path | None) -> str:
    return "" if path_value is None else str(path_value)


def detect_browser_executable(browser_channel: str) -> str | None:
    if os.name != "nt":
        return None

    home = Path.home()
    candidates: dict[str, list[Path]] = {
        "chrome": [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            home / "AppData" / "Local" / "Google" / "Chrome" / "Application" / "chrome.exe",
        ],
        "msedge": [
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            home / "AppData" / "Local" / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ],
    }

    for candidate in candidates.get(browser_channel, []):
        if candidate.exists():
            return str(candidate)
    return None


def normalize_cdp_url(cdp_url: str | None) -> str | None:
    if cdp_url is None:
        return None
    return cdp_url.rstrip("/")


def remote_debugging_port(cdp_url: str) -> int:
    parsed = urlparse(cdp_url)
    if parsed.port is None:
        raise ValueError("CDP URL must include an explicit port, such as http://127.0.0.1:9222.")
    return parsed.port


@dataclass
class RuntimePaths:
    repo_root: Path
    logs_dir: Path
    screenshots_dir: Path
    html_dir: Path
    state_dir: Path
    checkpoint_file: Path
    prompt_archive_file: Path

    @classmethod
    def create(cls, repo_root: Path) -> "RuntimePaths":
        logs_dir = repo_root / "logs"
        screenshots_dir = logs_dir / "screenshots"
        html_dir = logs_dir / "html"
        state_dir = repo_root / "state"
        checkpoint_file = state_dir / "checkpoint.json"
        prompt_archive_file = repo_root / "prompt_archive.json"

        for folder in (logs_dir, screenshots_dir, html_dir, state_dir):
            folder.mkdir(parents=True, exist_ok=True)

        if not prompt_archive_file.exists():
            prompt_archive_file.write_text("[]\n", encoding="utf-8")

        return cls(
            repo_root=repo_root,
            logs_dir=logs_dir,
            screenshots_dir=screenshots_dir,
            html_dir=html_dir,
            state_dir=state_dir,
            checkpoint_file=checkpoint_file,
            prompt_archive_file=prompt_archive_file,
        )


@dataclass
class Checkpoint:
    next_number: int
    last_successful_number: int | None
    last_draft_url: str
    last_action: str
    updated_at: str
    last_bottom_index: int | None = None
    last_bottom_href: str = ""
    skipped_draft_urls: list[str] = field(default_factory=list)


@dataclass
class ActionRecord:
    timestamp: str
    draft_number: int
    original_title: str
    new_title: str
    draft_url: str
    current_url: str
    action: str
    result: str
    posted_status: bool
    error_message: str
    screenshot_path: str
    html_path: str


@dataclass
class PromptArchiveRecord:
    timestamp: str
    draft_number: int
    draft_id: str
    draft_url: str
    new_title: str
    assigned_prompt_text: str
    original_prompt: str


@dataclass
class DraftTile:
    href: str
    x: float
    y: float
    locator: Locator
    data_index: int


@dataclass
class ProcessResult:
    draft_url: str
    original_title: str
    used_number: int
    new_title: str
    posted_status: bool
    result: str
    title_changed: bool = False
    error_message: str = ""


class RunLogger:
    def __init__(self, runtime_paths: RuntimePaths, run_id: str) -> None:
        self.runtime_paths = runtime_paths
        self.run_id = run_id
        self.jsonl_file = runtime_paths.logs_dir / f"run_{run_id}.jsonl"
        self.csv_file = runtime_paths.logs_dir / f"run_{run_id}.csv"
        self.text_file = runtime_paths.logs_dir / f"run_{run_id}.log"
        self._csv_header_written = self.csv_file.exists() and self.csv_file.stat().st_size > 0

    def _print(self, tag: str, style: str, message: str) -> None:
        line = f"[{tag}] {message}"
        console.print(f"[{style}]{line}[/{style}]")
        with self.text_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")

    def start(self, message: str) -> None:
        self._print("START", "bold cyan", message)

    def info(self, message: str) -> None:
        self._print("INFO", "white", message)

    def ok(self, message: str) -> None:
        self._print("OK", "green", message)

    def error(self, message: str) -> None:
        self._print("ERROR", "bold red", message)

    def warn(self, message: str) -> None:
        self._print("WARN", "bold yellow", message)

    def stop(self, message: str) -> None:
        self._print("STOP", "bold yellow", message)

    def record(self, action_record: ActionRecord) -> None:
        payload = asdict(action_record)
        with self.jsonl_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")

        with self.csv_file.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(payload.keys()))
            if not self._csv_header_written:
                writer.writeheader()
                self._csv_header_written = True
            writer.writerow(payload)

    def archive_prompt(self, archive_file: Path, prompt_record: PromptArchiveRecord) -> None:
        try:
            existing = json.loads(archive_file.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, FileNotFoundError):
            existing = []

        existing.append(asdict(prompt_record))
        archive_file.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


class SoraPoster:
    def __init__(
        self,
        *,
        base_url: str,
        start_number: int,
        max_posts: int,
        dry_run: bool,
        rename_only: bool,
        keep_existing_title: bool,
        headful: bool,
        slow_mo: int,
        browser_channel: str,
        browser_executable_path: str | None,
        cdp_url: str | None,
        launch_browser: bool,
        user_data_dir: Path,
        screenshot_on_success: bool,
        resume_from_checkpoint: bool,
        manual_ready: bool,
        auto_start_number: bool,
        runtime_paths: RuntimePaths,
        logger: RunLogger,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.start_number = start_number
        self.max_posts = max_posts
        self.dry_run = dry_run
        self.rename_only = rename_only
        self.keep_existing_title = keep_existing_title
        self.headful = headful
        self.slow_mo = slow_mo
        self.browser_channel = browser_channel
        self.browser_executable_path = browser_executable_path
        self.cdp_url = normalize_cdp_url(cdp_url)
        self.launch_browser = launch_browser
        self.user_data_dir = user_data_dir
        self.screenshot_on_success = screenshot_on_success
        self.resume_from_checkpoint = resume_from_checkpoint
        self.manual_ready = manual_ready
        self.auto_start_number = auto_start_number
        self.runtime_paths = runtime_paths
        self.logger = logger

        self.attempted = 0
        self.renamed = 0
        self.posted = 0
        self.failed = 0
        self.skipped = 0
        self.processed_urls: set[str] = set()
        self.skipped_draft_urls: set[str] = set()
        self.last_confirmed_bottom_index: int | None = None
        self.last_confirmed_bottom_href: str = ""
        self.resume_floor_adjustment = 0

    def run(self) -> int:
        number = self.start_number
        if self.resume_from_checkpoint:
            checkpoint = self.load_checkpoint()
            if checkpoint is not None:
                number = checkpoint.next_number
                self.last_confirmed_bottom_index = checkpoint.last_bottom_index
                self.last_confirmed_bottom_href = checkpoint.last_bottom_href
                self.skipped_draft_urls = set(checkpoint.skipped_draft_urls)
                self.resume_floor_adjustment = 1 if checkpoint.last_action == "posted" else 0
                self.logger.info(
                    f"Resuming from checkpoint with next number {make_title(number)}."
                )
                if checkpoint.last_bottom_index is not None:
                    self.logger.info(
                        f"Loaded bottom watermark from checkpoint: data-index {checkpoint.last_bottom_index}."
                    )

        self.logger.start(
            f"Run started at {now_stamp()} with next title {make_title(number)}."
        )
        self.logger.info(f"Using repo root {self.runtime_paths.repo_root}.")
        if self.cdp_url:
            self.logger.info(f"Connecting to existing browser over CDP at {self.cdp_url}.")
        else:
            self.logger.info(f"Using browser profile {self.user_data_dir}.")

        browser_executable = self.browser_executable_path or detect_browser_executable(
            self.browser_channel
        )
        if browser_executable:
            self.logger.info(f"Using browser executable {browser_executable}.")
        elif not self.cdp_url:
            self.logger.info(f"Using Playwright browser channel {self.browser_channel}.")

        if self.launch_browser:
            cdp_url = self.cdp_url or "http://127.0.0.1:9222"
            self.cdp_url = cdp_url
            if self.cdp_ready(cdp_url):
                self.logger.ok("Remote debugging endpoint is already ready. Reusing the existing browser session.")
            else:
                if browser_executable is None:
                    raise RuntimeError(
                        "Could not find the requested browser executable. Provide --browser-executable-path explicitly."
                    )
                self.launch_debug_browser(browser_executable, cdp_url)
                self.logger.info(f"Waiting for remote debugging at {cdp_url}.")
                self.wait_for_cdp_ready(cdp_url)
                self.logger.ok("Remote debugging endpoint is ready.")

        with sync_playwright() as playwright:
            owned_browser = None
            if self.cdp_url:
                owned_browser = playwright.chromium.connect_over_cdp(self.cdp_url)
                context = owned_browser.contexts[0] if owned_browser.contexts else owned_browser.new_context()
                context.set_default_timeout(GENERAL_TIMEOUT_MS)
                page = context.pages[0] if context.pages else context.new_page()
            else:
                browser_executable = self.browser_executable_path or detect_browser_executable(
                    self.browser_channel
                )
                launch_kwargs: dict[str, object] = {
                    "user_data_dir": str(self.user_data_dir),
                    "headless": not self.headful,
                    "slow_mo": self.slow_mo,
                }
                if browser_executable:
                    launch_kwargs["executable_path"] = browser_executable
                else:
                    launch_kwargs["channel"] = self.browser_channel

                context = playwright.chromium.launch_persistent_context(
                    **launch_kwargs,
                )
            context.set_default_timeout(GENERAL_TIMEOUT_MS)
            page = context.pages[0] if context.pages else context.new_page()
            try:
                if self.manual_ready:
                    page = self.wait_for_manual_ready(context, page)
                else:
                    page.goto(self.base_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1_000)
                    self.ensure_logged_in(page)

                number = self.adjust_start_number_from_profile_feed(page, number)
                self.open_drafts_page(page)
                number = self.adjust_start_number_from_visible_titles(page, number)

                while self.posted < self.max_posts and self.attempted < self.max_posts:
                    target_title = self.describe_target_title(number)
                    self.attempted += 1
                    self.logger.info(f"Processing title {target_title}.")
                    try:
                        process_result = self.process_one(page, number)
                    except DraftsDepletedError:
                        self.attempted -= 1
                        self.logger.ok(
                            "No remaining unprocessed draft tiles were found. Ending the run cleanly."
                        )
                        break
                    except Exception as exc:
                        self.failed += 1
                        self.handle_failure(page, target_title, exc)
                        self.logger.error(str(exc))
                        self.logger.stop("Halting run due to unexpected UI state.")
                        return 1

                    self.processed_urls.add(process_result.draft_url)
                    if not self.dry_run and process_result.title_changed:
                        self.renamed += 1
                    if process_result.result == "skipped":
                        self.skipped += 1
                        self.skipped_draft_urls.add(process_result.draft_url)
                    elif not self.dry_run and not self.rename_only:
                        self.posted += 1

                    if not self.dry_run:
                        self.write_checkpoint(
                            Checkpoint(
                                next_number=process_result.used_number + 1,
                                last_successful_number=process_result.used_number,
                                last_draft_url=process_result.draft_url,
                                last_action="skipped_post" if process_result.result == "skipped" else "rename_only" if self.rename_only else "posted",
                                updated_at=now_stamp(),
                                last_bottom_index=self.last_confirmed_bottom_index,
                                last_bottom_href=self.last_confirmed_bottom_href,
                                skipped_draft_urls=sorted(self.skipped_draft_urls),
                            )
                        )
                    number = process_result.used_number + 1

                    if self.dry_run or self.rename_only:
                        self.return_to_drafts_page(page)

            finally:
                if owned_browser is None:
                    context.close()

        self.logger.ok(
            "Run finished. "
            f"Attempted={self.attempted}, renamed={self.renamed}, posted={self.posted}, skipped={self.skipped}, failed={self.failed}."
        )
        return 0

    def launch_debug_browser(self, browser_executable: str, cdp_url: str) -> None:
        port = remote_debugging_port(cdp_url)
        args = [
            browser_executable,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={self.user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        creationflags = 0
        popen_kwargs: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "start_new_session": True,
        }
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
            popen_kwargs["creationflags"] = creationflags

        subprocess.Popen(args, **popen_kwargs)
        self.logger.info(
            "Launched browser in remote-debugging mode. Complete any security/login steps in that browser window."
        )

    def wait_for_cdp_ready(self, cdp_url: str, timeout_seconds: int = 20) -> None:
        deadline = time.time() + timeout_seconds
        version_url = f"{cdp_url}/json/version"
        last_error = ""
        while time.time() < deadline:
            try:
                with urlopen(version_url, timeout=2) as response:
                    if response.status == 200:
                        return
            except URLError as exc:
                last_error = str(exc)
            time.sleep(0.5)

        raise RuntimeError(
            f"Chrome remote debugging was not reachable at {cdp_url}. Last error: {last_error or 'no response'}"
        )

    def cdp_ready(self, cdp_url: str) -> bool:
        version_url = f"{cdp_url}/json/version"
        try:
            with urlopen(version_url, timeout=1.5) as response:
                return response.status == 200
        except URLError:
            return False

    def process_one(self, page: Page, requested_number: int) -> ProcessResult:
        requested_title = self.describe_target_title(requested_number)
        draft_tile = self.select_next_tile(page)
        full_draft_url = self.full_url(draft_tile.href)
        draft_id = self.extract_draft_id(full_draft_url)
        self.logger.info(f"Opening draft {full_draft_url}.")
        draft_tile.locator.click()
        self.wait_for_draft_detail(page, full_draft_url)
        self.ensure_draft_ready_for_edit(page, full_draft_url)

        if self.keep_existing_title:
            original_title, used_title, used_number = self.keep_current_title(page, requested_number)
            title_changed = False
        else:
            original_title, used_title, used_number = self.rename_current_draft(page, requested_number)
            title_changed = True
        self.archive_prompt(
            draft_number=used_number,
            draft_id=draft_id,
            draft_url=full_draft_url,
            new_title=used_title,
            original_prompt=original_title,
        )
        posted_status = False

        if self.dry_run:
            self.logger.ok("Dry-run mode active. Skipping save and post.")
            result = "success"
            error_message = ""
        elif self.rename_only:
            self.logger.ok("Rename-only mode active. Skipping post.")
            result = "success"
            error_message = ""
        else:
            posted_status, error_message = self.post_current_draft(page)
            if posted_status:
                result = "success"
                self.logger.ok("Draft posted successfully.")
            else:
                result = "skipped"
                self.logger.warn(f"Post skipped for {used_title}: {error_message}")
                self.return_to_drafts_page(page)

        screenshot_path = None
        if self.screenshot_on_success:
            screenshot_path, _ = self.capture_artifacts(
                page,
                prefix=f"success_{used_title}",
                include_html=False,
            )
            self.logger.ok(f"Success screenshot saved to {screenshot_path}.")

        self.logger.record(
            ActionRecord(
                timestamp=now_stamp(),
                draft_number=used_number,
                original_title=original_title,
                new_title=used_title,
                draft_url=full_draft_url,
                current_url=page.url,
                action="dry_run" if self.dry_run else "rename_only" if self.rename_only else "post",
                result=result,
                posted_status=posted_status,
                error_message=error_message,
                screenshot_path=serialize_path(screenshot_path),
                html_path="",
            )
        )

        if used_title != requested_title:
            self.logger.info(
                f"Adjusted requested title from {requested_title} to {used_title} based on detail-page safety checks."
            )

        return ProcessResult(
            draft_url=full_draft_url,
            original_title=original_title,
            used_number=used_number,
            new_title=used_title,
            posted_status=posted_status,
            result=result,
            title_changed=title_changed,
            error_message=error_message,
        )

    def describe_target_title(self, requested_number: int) -> str:
        if self.keep_existing_title:
            return "(keeping existing title)"
        return make_title(requested_number)

    def archive_prompt(
        self,
        *,
        draft_number: int,
        draft_id: str,
        draft_url: str,
        new_title: str,
        original_prompt: str,
    ) -> None:
        self.logger.archive_prompt(
            self.runtime_paths.prompt_archive_file,
            PromptArchiveRecord(
                timestamp=now_stamp(),
                draft_number=draft_number,
                draft_id=draft_id,
                draft_url=draft_url,
                new_title=new_title,
                assigned_prompt_text=new_title,
                original_prompt=original_prompt,
            ),
        )

    def handle_failure(self, page: Page, new_title: str, exc: Exception) -> None:
        screenshot_path: Path | None = None
        html_path: Path | None = None
        try:
            screenshot_path, html_path = self.capture_artifacts(
                page,
                prefix=f"error_{new_title}",
                include_html=True,
            )
            self.logger.error(f"Screenshot saved to {screenshot_path}.")
            if html_path is not None:
                self.logger.error(f"HTML saved to {html_path}.")
        except Exception as artifact_exc:
            self.logger.error(f"Artifact capture failed: {artifact_exc}")

        self.logger.record(
            ActionRecord(
                timestamp=now_stamp(),
                draft_number=int(new_title.split("_")[1]),
                original_title=self.try_read_current_title(page),
                new_title=new_title,
                draft_url=page.url,
                current_url=page.url,
                action="post" if not (self.dry_run or self.rename_only) else "rename_only" if self.rename_only else "dry_run",
                result="error",
                posted_status=False,
                error_message=str(exc),
                screenshot_path=serialize_path(screenshot_path),
                html_path=serialize_path(html_path),
            )
        )

    def ensure_logged_in(self, page: Page) -> None:
        page.wait_for_timeout(500)
        if self.is_logged_in(page):
            return

        if not self.headful:
            raise RuntimeError(
                "The browser profile is not logged in to Sora. Run in headful mode, sign in once, then rerun."
            )

        self.logger.info(
            "Browser profile is not logged in yet. Log in to Sora in the opened browser window."
        )

        while True:
            try:
                input("Press Enter here after you finish logging in to Sora in the browser window...")
            except EOFError:
                page.wait_for_timeout(5_000)

            if self.is_logged_in(page):
                self.logger.ok("Login detected. Continuing with the run.")
                return

            self.logger.info("Still not logged in. Finish the login flow in the browser, then press Enter again.")

    def wait_for_manual_ready(self, context, page: Page) -> Page:
        if not self.headful:
            raise RuntimeError(
                "--manual-ready requires headful mode so you can use the browser window."
            )

        ready_page = self.find_logged_in_page(context)
        if ready_page is not None:
            self.logger.ok(
                "Found an existing logged-in Sora session. Continuing without waiting for manual confirmation."
            )
            return ready_page

        self.logger.info(
            "Manual-ready mode: use the opened browser window to go to Sora, complete any security check, and log in."
        )

        if page.url in ("about:blank", ""):
            page.goto("about:blank")

        while True:
            try:
                input("After Sora is fully logged in in the browser, press Enter here to continue...")
            except EOFError:
                page.wait_for_timeout(5_000)

            ready_page = self.find_logged_in_page(context)
            if ready_page is not None:
                self.logger.ok("Logged-in Sora session detected. Opening Drafts.")
                return ready_page

            self.logger.info(
                "The browser does not appear to have a logged-in Sora app page yet. Finish the security/login flow in the browser, then press Enter again."
            )

    def is_logged_in(self, page: Page) -> bool:
        current_url = page.url.lower()
        if "/auth" in current_url or "login" in current_url:
            return False

        try:
            if page.locator("a[href='/drafts']").count():
                return True
            if page.locator("a[href$='/drafts']").count():
                return True
            if page.locator("a[href='/explore']").count():
                return True
            if page.locator("a[href='/profile']").count():
                return True
            if page.locator(TILE_SELECTOR).count():
                return True
        except PlaywrightError:
            return False

        return "/drafts" in current_url or "/d/" in current_url

    def is_ready_on_drafts(self, page: Page) -> bool:
        current_url = page.url.lower()
        if "/drafts" in current_url:
            return True

        try:
            if page.locator(TILE_SELECTOR).count():
                return True
        except PlaywrightError:
            return False

        return False

    def find_logged_in_page(self, context) -> Page | None:
        for candidate in context.pages:
            if self.is_logged_in(candidate):
                return candidate
        return None

    def adjust_start_number_from_profile_feed(self, page: Page, requested_number: int) -> int:
        self.logger.info("Checking the Profile feed for the most recent posted numbered title.")
        original_url = page.url
        try:
            if self.is_profile_post_page(page):
                latest_number = self.wait_for_visible_numbered_title(page, timeout_seconds=5)
                if latest_number is not None:
                    return self.apply_profile_number_suggestion(latest_number, requested_number)
                self.logger.info(
                    "Already on a profile post detail page, but no numbered draft_000000-style title was visible there."
                )

            if "/profile" not in page.url:
                page.goto(f"{self.base_url}/profile", wait_until="domcontentloaded")
                page.wait_for_timeout(1_000)
            else:
                self.logger.info("Already on the Profile page. Reusing the current view.")

            self.prepare_profile_feed(page)
            latest_tile = self.select_profile_top_left_tile(page)
            full_post_url = self.full_url(latest_tile.href)
            self.logger.info(f"Opening latest visible profile post {full_post_url}.")
            latest_tile.locator.click()
            page.wait_for_timeout(1_000)

            latest_number = self.wait_for_visible_numbered_title(page)
            if latest_number is None:
                self.logger.info(
                    "No numbered draft_000000-style title was detected on the latest visible profile post."
                )
                return requested_number

            return self.apply_profile_number_suggestion(latest_number, requested_number)
        except Exception as exc:
            self.logger.warn(
                f"Profile preflight could not confirm the latest posted number: {exc}. Continuing with checkpoint and draft-detail safeguards."
            )
            return requested_number
        finally:
            if "/drafts" not in original_url and page.url != original_url:
                try:
                    page.goto(original_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(500)
                except PlaywrightError:
                    pass

    def apply_profile_number_suggestion(self, latest_number: int, requested_number: int) -> int:
        latest_title = make_title(latest_number)
        suggested_number = latest_number + 1
        suggested_title = make_title(suggested_number)
        self.logger.info(
            f"Latest visible profile post appears to be {latest_title}. Suggested next number is {suggested_title}."
        )

        if requested_number < suggested_number:
            self.logger.warn(
                f"Requested start number {make_title(requested_number)} is not the next safe slot after the latest visible profile post {latest_title}. {suggested_title} appears safer."
            )
            if self.auto_start_number:
                self.logger.ok(
                    f"Auto-start enabled. Using {suggested_title} instead of {make_title(requested_number)}."
                )
                return suggested_number

        return requested_number

    def is_profile_post_page(self, page: Page) -> bool:
        current_url = page.url.lower()
        return "/p/" in current_url

    def prepare_profile_feed(self, page: Page) -> None:
        try:
            page.mouse.wheel(0, -20_000)
        except PlaywrightError:
            pass
        page.wait_for_timeout(500)

        if self.visible_profile_tiles(page):
            return

        self.logger.info("Profile feed tiles are not visible yet. Waiting for the feed to finish loading.")
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.visible_profile_tiles(page):
                return
            page.wait_for_timeout(500)

        self.logger.info("Profile feed still looks empty. Reloading the Profile page once and checking again.")
        page.goto(f"{self.base_url}/profile", wait_until="domcontentloaded")
        page.wait_for_timeout(1_500)
        try:
            page.mouse.wheel(0, -20_000)
        except PlaywrightError:
            pass
        page.wait_for_timeout(500)

    def select_profile_top_left_tile(self, page: Page) -> DraftTile:
        tiles = self.visible_profile_tiles(page)
        if not tiles:
            raise RuntimeError("No visible profile post tiles were found on the Profile page.")

        tiles.sort(key=lambda item: (item.y, item.x, item.data_index))
        return tiles[0]

    def visible_profile_tiles(self, page: Page) -> list[DraftTile]:
        tiles = self.visible_grid_tiles(page, "a[href]")
        if tiles:
            return tiles

        fallback_tiles: list[DraftTile] = []
        locator = page.locator("a[href]")
        count = locator.count()
        for index in range(count):
            link = locator.nth(index)
            try:
                if not link.is_visible():
                    continue
                href = link.get_attribute("href")
                box = link.bounding_box()
                has_media = link.locator("video, img").count() > 0
            except PlaywrightError:
                continue

            if not href or not box or not has_media:
                continue
            if not self.is_profile_post_href(href):
                continue
            if box["width"] < 80 or box["height"] < 80:
                continue

            fallback_tiles.append(
                DraftTile(
                    href=href,
                    x=box["x"],
                    y=box["y"],
                    locator=link,
                    data_index=-1,
                )
            )

        if fallback_tiles:
            self.logger.info(
                f"Using fallback profile tile detection with {len(fallback_tiles)} visible media link(s)."
            )
        return fallback_tiles

    def is_profile_post_href(self, href: str) -> bool:
        lowered = href.lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            parsed = urlparse(lowered)
            path = parsed.path
        else:
            path = lowered

        if not path.startswith("/"):
            return False
        if path in {"/explore", "/drafts", "/profile", "/activity", "/search"}:
            return False
        if path.startswith("/d/"):
            return True
        return path.count("/") >= 2

    def adjust_start_number_from_visible_titles(self, page: Page, requested_number: int) -> int:
        visible_numbers = self.visible_numbered_titles(page)
        if not visible_numbers:
            self.logger.info(
                "No visible draft_000000-style titles were detected on the current page, so no automatic start-number suggestion is available."
            )
            return requested_number

        highest_seen = max(visible_numbers)
        suggested_number = highest_seen + 1
        self.logger.info(
            f"Visible numbered titles were detected through {make_title(highest_seen)}. Suggested next number is {make_title(suggested_number)}."
        )

        if requested_number < suggested_number:
            self.logger.warn(
                f"Requested start number {make_title(requested_number)} looks behind visible titles. {make_title(suggested_number)} appears to be safer."
            )
            if self.auto_start_number:
                self.logger.ok(
                    f"Auto-start enabled. Using {make_title(suggested_number)} instead of {make_title(requested_number)}."
                )
                return suggested_number

        return requested_number

    def visible_numbered_titles(self, page: Page) -> list[int]:
        visible_numbers: set[int] = set()
        try:
            body_text = page.locator("body").inner_text(timeout=5_000)
        except PlaywrightError:
            body_text = ""

        for match in NUMBERED_TITLE_RE.finditer(body_text):
            try:
                visible_numbers.add(int(match.group(1)))
            except ValueError:
                continue

        try:
            current_title = self.try_read_current_title(page)
        except PlaywrightError:
            current_title = ""

        for match in NUMBERED_TITLE_RE.finditer(current_title):
            try:
                visible_numbers.add(int(match.group(1)))
            except ValueError:
                continue

        return sorted(visible_numbers)

    def wait_for_visible_numbered_title(self, page: Page, timeout_seconds: int = 8) -> int | None:
        deadline = time.time() + timeout_seconds
        latest_number: int | None = None
        while time.time() < deadline:
            latest_number = self.extract_numbered_title_from_page(page)
            if latest_number is not None:
                return latest_number
            page.wait_for_timeout(400)
        return latest_number

    def extract_numbered_title_from_page(self, page: Page) -> int | None:
        try:
            body_text = page.locator("body").inner_text(timeout=5_000)
        except PlaywrightError:
            body_text = ""
        return self.extract_numbered_title(body_text)

    def select_next_tile(self, page: Page) -> DraftTile:
        self.open_drafts_page(page)
        self.scroll_to_bottom(page)

        for _ in range(12):
            tiles = self.visible_tiles(page)
            for tile in tiles:
                full_tile_url = self.full_url(tile.href)
                if full_tile_url in self.skipped_draft_urls:
                    continue
                if full_tile_url not in self.processed_urls:
                    self.logger.info(
                        f"Selected draft tile {full_tile_url} at data-index {tile.data_index}."
                    )
                    return tile
            page.mouse.wheel(0, -3_000)
            page.wait_for_timeout(600)

        raise DraftsDepletedError("No unprocessed draft tile was found on the drafts page.")

    def open_drafts_page(self, page: Page) -> None:
        if "/drafts" not in page.url:
            self.logger.info("Opening Drafts page.")
            page.goto(f"{self.base_url}/drafts", wait_until="domcontentloaded")
            page.wait_for_timeout(1_000)
            self.logger.ok(f"Drafts page opened at {page.url}.")
        self.logger.info("Waiting for draft tiles to appear.")
        page.wait_for_selector(TILE_SELECTOR)
        self.logger.ok("Draft tiles are visible.")

    def return_to_drafts_page(self, page: Page) -> None:
        if "/drafts" in page.url:
            return

        self.logger.info("Returning to Drafts page.")
        try:
            page.go_back(wait_until="domcontentloaded", timeout=GENERAL_TIMEOUT_MS)
            page.wait_for_timeout(800)
            if "/drafts" in page.url:
                page.wait_for_selector(TILE_SELECTOR)
                self.logger.ok("Returned to Drafts using browser history.")
                return
        except PlaywrightTimeoutError:
            pass
        except PlaywrightError:
            pass

        self.logger.info("History return was not usable. Reloading Drafts directly.")
        page.goto(f"{self.base_url}/drafts", wait_until="domcontentloaded")
        page.wait_for_timeout(1_000)
        self.ensure_logged_in(page)
        self.logger.ok(f"Drafts page opened at {page.url}.")

    def scroll_to_bottom(self, page: Page) -> None:
        stable_marker: tuple[int, str] | None = None
        best_max_index = -1
        plateau_cycles = 0
        target_floor = self.expected_bottom_floor()
        self.logger.info("Scrolling to the bottom of the Drafts grid.")
        if target_floor is not None:
            self.logger.info(
                f"Using bottom watermark targeting data-index >= {target_floor}."
            )
            best_max_index = self.fast_reacquire_bottom_region(page, target_floor)
            marker = self.bottom_marker(page)
            if marker is not None:
                stable_marker = marker
                if marker[0] > best_max_index:
                    best_max_index = marker[0]
        for _ in range(40):
            self.scroll_all_the_way_down(page)
            loaded_max_index = self.wait_for_additional_drafts(
                page,
                best_max_index,
                target_floor=target_floor,
            )
            marker = self.bottom_marker(page)
            if marker is not None and marker != stable_marker:
                self.logger.info(
                    f"Bottom candidate now looks like data-index {marker[0]}."
                )

            if loaded_max_index > best_max_index:
                best_max_index = loaded_max_index
                plateau_cycles = 0
                stable_marker = marker
                self.logger.info(
                    f"Loaded deeper drafts through data-index {best_max_index}. Continuing to wait for more."
                )
            else:
                if marker is not None and marker == stable_marker:
                    plateau_cycles += 1
                else:
                    stable_marker = marker
                    plateau_cycles = 1
                required_cycles = 2 if target_floor is not None and best_max_index >= target_floor else 4
                self.logger.info(
                    f"Bottom load appears unchanged at data-index {best_max_index}. Stable cycle {plateau_cycles}/{required_cycles}."
                )
                if best_max_index >= 0 and plateau_cycles >= required_cycles and marker is not None:
                    self.last_confirmed_bottom_index = marker[0]
                    self.last_confirmed_bottom_href = self.full_url(marker[1])
                    self.logger.ok(
                        f"Reached the bottom of the Drafts grid near data-index {marker[0]} after waiting for the load to plateau."
                    )
                    return

        raise RuntimeError(
            "Unable to positively confirm the bottom of the Drafts grid. Stopping to avoid renaming or posting the wrong draft."
        )

    def expected_bottom_floor(self) -> int | None:
        if self.last_confirmed_bottom_index is None:
            return None
        reduction = self.resume_floor_adjustment + self.posted
        return max(0, self.last_confirmed_bottom_index - reduction - 2)

    def fast_reacquire_bottom_region(self, page: Page, target_floor: int) -> int:
        highest_seen = self.highest_visible_index(page)
        if highest_seen >= target_floor:
            self.logger.ok(
                f"Bottom watermark is already visible near data-index {highest_seen}."
            )
            return highest_seen

        self.logger.info(
            f"Fast reacquire: jumping back toward the known bottom zone near data-index {target_floor}."
        )
        stagnant_cycles = 0
        for _ in range(18):
            self.scroll_all_the_way_down(page)
            page.wait_for_timeout(300)
            current_highest = self.highest_visible_index(page)
            if current_highest > highest_seen:
                highest_seen = current_highest
                stagnant_cycles = 0
                self.logger.info(
                    f"Fast reacquire reached visible data-index {highest_seen}."
                )
                if current_highest >= target_floor:
                    self.logger.ok(
                        f"Fast reacquire reached the known bottom zone near data-index {current_highest}."
                    )
                    return current_highest
            else:
                stagnant_cycles += 1
                if stagnant_cycles >= 3:
                    break

        self.logger.info(
            f"Fast reacquire stopped near visible data-index {highest_seen}. Falling back to the patient bottom check."
        )
        return highest_seen

    def wait_for_additional_drafts(
        self,
        page: Page,
        baseline_max_index: int,
        *,
        target_floor: int | None,
    ) -> int:
        highest_seen = max(baseline_max_index, self.highest_visible_index(page))
        started_at = time.time()
        last_progress_at = started_at
        idle_threshold = 1.2 if target_floor is not None and highest_seen >= target_floor else 2.5

        while time.time() - started_at < 10:
            page.wait_for_timeout(700)
            current_highest = self.highest_visible_index(page)
            if current_highest > highest_seen:
                highest_seen = current_highest
                last_progress_at = time.time()
                idle_threshold = 1.2 if target_floor is not None and highest_seen >= target_floor else 2.5
                self.logger.info(
                    f"Drafts list extended to visible data-index {highest_seen}."
                )
                continue

            if time.time() - last_progress_at >= idle_threshold:
                break

        return highest_seen

    def highest_visible_index(self, page: Page) -> int:
        tiles = self.visible_tiles(page)
        if not tiles:
            return -1
        return max(tile.data_index for tile in tiles)

    def visible_tiles(self, page: Page) -> list[DraftTile]:
        tiles = self.visible_grid_tiles(page, TILE_SELECTOR)
        tiles.sort(key=lambda item: (item.data_index, item.y), reverse=True)
        return tiles

    def visible_grid_tiles(self, page: Page, link_selector: str) -> list[DraftTile]:
        locator = page.locator("[data-index]").filter(has=page.locator(link_selector))
        count = locator.count()
        tiles: list[DraftTile] = []
        for index in range(count):
            container = locator.nth(index)
            try:
                if not container.is_visible():
                    continue
                box = container.bounding_box()
                href = container.locator(link_selector).first.get_attribute("href")
                data_index_attr = container.get_attribute("data-index")
            except PlaywrightError:
                continue
            if not box or not href:
                continue
            try:
                data_index = int(data_index_attr or "-1")
            except ValueError:
                data_index = -1
            tiles.append(
                DraftTile(
                    href=href,
                    x=box["x"],
                    y=box["y"],
                    locator=container.locator(link_selector).first,
                    data_index=data_index,
                )
            )
        return tiles

    def bottom_marker(self, page: Page) -> tuple[int, str] | None:
        tiles = self.visible_tiles(page)
        if not tiles:
            return None
        tile = tiles[0]
        return tile.data_index, tile.href

    def scroll_all_the_way_down(self, page: Page) -> None:
        page.keyboard.press("End")
        page.evaluate(
            """
            () => {
              const root = document.scrollingElement || document.documentElement || document.body;
              if (root) {
                root.scrollTop = root.scrollHeight;
              }
              window.scrollTo(0, document.body.scrollHeight);
              for (const el of Array.from(document.querySelectorAll('*'))) {
                const style = window.getComputedStyle(el);
                const overflowY = style.overflowY;
                const canScroll =
                  (overflowY === 'auto' || overflowY === 'scroll') &&
                  el.scrollHeight > el.clientHeight + 100;
                if (canScroll) {
                  el.scrollTop = el.scrollHeight;
                }
              }
            }
            """
        )
        page.mouse.wheel(0, 40_000)

    def wait_for_draft_detail(self, page: Page, expected_url: str) -> None:
        page.wait_for_url(re.compile(r".*/d/[^/?#]+"), timeout=GENERAL_TIMEOUT_MS)
        if not page.url.startswith(expected_url):
            self.logger.info(f"Detail page URL is {page.url}.")
        self.logger.ok(f"Loaded draft detail page {page.url}.")

    def ensure_draft_ready_for_edit(self, page: Page, draft_url: str) -> None:
        if self.has_visible_prompt_edit_icon(page):
            return

        self.logger.warn(
            "Draft detail loaded without the prompt pencil icon. Waiting briefly to see if Sora finishes rendering."
        )
        page.wait_for_timeout(1_500)
        if self.has_visible_prompt_edit_icon(page):
            self.logger.ok("Draft detail finished rendering after a short wait.")
            return

        for attempt in range(1, 3):
            if self.is_draft_loading_state(page):
                self.logger.warn(
                    f"Draft detail still looks stuck on a loading state. Refreshing the draft page (attempt {attempt}/2)."
                )
            else:
                self.logger.warn(
                    f"Draft detail still does not expose the prompt pencil icon. Reopening the same draft (attempt {attempt}/2)."
                )

            try:
                page.goto(draft_url, wait_until="domcontentloaded")
            except PlaywrightError:
                page.wait_for_timeout(1_000)
                continue

            page.wait_for_timeout(1_500)
            self.wait_for_draft_detail(page, draft_url)
            if self.has_visible_prompt_edit_icon(page):
                self.logger.ok("Recovered the draft detail page and found the prompt pencil icon.")
                return

        raise RuntimeError(
            "The draft detail page loaded, but the prompt pencil icon never appeared after retrying the draft."
        )

    def has_visible_prompt_edit_icon(self, page: Page) -> bool:
        try:
            self.find_icon_button(page, path_fragment="M16.585 11l-3.586-3.585-5.843 5.842")
            return True
        except RuntimeError:
            return False

    def is_draft_loading_state(self, page: Page) -> bool:
        try:
            return page.locator(".spin_loader").count() > 0 and page.locator(".spin_loader").first.is_visible()
        except PlaywrightError:
            return False

    def rename_current_draft(self, page: Page, requested_number: int) -> tuple[str, str, int]:
        edit_button = self.find_icon_button(
            page, path_fragment="M16.585 11l-3.586-3.585-5.843 5.842"
        )
        edit_button.click()
        self.logger.ok("Edit icon clicked.")

        caption = page.locator(CAPTION_SELECTOR).first
        caption.wait_for(state="visible")
        original_title = caption.input_value().strip()
        safe_number = self.adjust_number_from_detail_title(original_title, requested_number)
        new_title = make_title(safe_number)
        self.logger.info(f'Current prompt text: "{original_title}"')
        self.logger.info(f'New prompt text: "{new_title}"')

        caption.click()
        caption.press("ControlOrMeta+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(150)
        if caption.input_value().strip():
            caption.fill("")
        caption.fill(new_title)

        if caption.input_value().strip() != new_title:
            raise RuntimeError("The prompt field did not accept the new value.")
        self.logger.ok("New prompt text entered.")

        if self.dry_run:
            return original_title, new_title, safe_number

        save_button = self.find_icon_button(page, path_fragment="5.625 8.25")
        if not save_button.is_enabled():
            raise RuntimeError("Save button is disabled unexpectedly.")
        save_button.click()
        self.logger.ok("Save button clicked.")

        self.verify_saved_title(page, new_title)
        self.logger.ok("Prompt save confirmed.")
        return original_title, new_title, safe_number

    def keep_current_title(self, page: Page, requested_number: int) -> tuple[str, str, int]:
        edit_button = self.find_icon_button(
            page, path_fragment="M16.585 11l-3.586-3.585-5.843 5.842"
        )
        edit_button.click()
        self.logger.ok("Edit icon clicked.")

        caption = page.locator(CAPTION_SELECTOR).first
        caption.wait_for(state="visible")
        original_title = caption.input_value().strip()
        used_title = original_title or f"untitled_draft_{requested_number:06d}"
        self.logger.info(f'Current prompt text: "{original_title}"')
        self.logger.info(f'Keeping existing prompt text: "{used_title}"')

        if self.dry_run:
            return original_title, used_title, requested_number

        save_button = self.find_icon_button(page, path_fragment="5.625 8.25")
        if not save_button.is_enabled():
            raise RuntimeError("Save button is disabled unexpectedly.")
        save_button.click()
        self.logger.ok("Save button clicked.")
        self.verify_saved_title(page, used_title)
        self.logger.ok("Prompt save confirmed.")
        return original_title, used_title, requested_number

    def adjust_number_from_detail_title(self, original_title: str, requested_number: int) -> int:
        current_number = self.extract_numbered_title(original_title)
        if current_number is None:
            return requested_number

        current_title = make_title(current_number)
        requested_title = make_title(requested_number)
        next_title = make_title(current_number + 1)
        if requested_number == current_number:
            self.logger.info(
                f"Detail-page title is already {current_title}. Reusing that slot for this draft and continuing with {next_title} next."
            )
            return requested_number

        if requested_number < current_number:
            self.logger.warn(
                f"Requested start number {requested_title} is behind the current draft title {current_title}. This draft is already occupying that later numbered slot."
            )
            if self.auto_start_number:
                self.logger.ok(
                    f"Auto-start enabled. Reusing {current_title} for this draft and continuing with {next_title} next instead of forcing {requested_title}."
                )
                return current_number
            raise RuntimeError(
                f"Requested start number {requested_title} is behind the current draft title {current_title}. "
                "Rerun with a higher --start-number or add --auto-start-number."
            )

        return requested_number

    def extract_numbered_title(self, text: str) -> int | None:
        matches = list(NUMBERED_TITLE_RE.finditer(text))
        if not matches:
            return None
        try:
            return max(int(match.group(1)) for match in matches)
        except ValueError:
            return None

    def verify_saved_title(self, page: Page, new_title: str) -> None:
        edit_button = self.find_icon_button(
            page, path_fragment="M16.585 11l-3.586-3.585-5.843 5.842"
        )
        deadline = time.time() + 15
        last_seen_value = ""

        while time.time() < deadline:
            page.wait_for_timeout(400)
            try:
                if not edit_button.is_visible():
                    continue
            except PlaywrightError:
                continue

            edit_button.click()
            self.logger.info("Reopened the prompt editor with the small pencil icon to verify the saved textbox value.")
            caption = page.locator(CAPTION_SELECTOR).first
            try:
                caption.wait_for(state="visible", timeout=GENERAL_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                continue

            try:
                last_seen_value = caption.input_value().strip()
            except PlaywrightError:
                last_seen_value = ""

            self.exit_edit_mode(page)

            if last_seen_value == new_title:
                return

            raise RuntimeError(
                "Saved prompt text did not persist after reopening Edit. "
                f"The textbox still contains: {last_seen_value[:160]!r}"
            )

        raise RuntimeError("Saved prompt text could not be verified by reopening Edit after clicking the checkmark.")

    def exit_edit_mode(self, page: Page) -> None:
        page.keyboard.press("Escape")
        try:
            page.get_by_role("button", name=re.compile(r"^Post$", re.I)).wait_for(
                state="visible",
                timeout=GENERAL_TIMEOUT_MS,
            )
            return
        except PlaywrightTimeoutError:
            pass

        try:
            close_button = page.get_by_role("button", name=re.compile(r"close", re.I)).first
            if close_button.is_visible():
                close_button.click()
                page.get_by_role("button", name=re.compile(r"^Post$", re.I)).wait_for(
                    state="visible",
                    timeout=GENERAL_TIMEOUT_MS,
                )
                return
        except PlaywrightError:
            pass

    def try_read_current_title(self, page: Page) -> str:
        caption = page.locator(CAPTION_SELECTOR)
        if not caption.count():
            return ""
        try:
            return caption.first.input_value().strip()
        except PlaywrightError:
            return ""

    def post_current_draft(self, page: Page) -> tuple[bool, str]:
        post_button = page.get_by_role("button", name=re.compile(r"^Post$", re.I))
        post_button.wait_for(state="visible")
        if not post_button.is_enabled():
            raise RuntimeError("Post button is disabled unexpectedly.")
        post_button.click()
        self.logger.ok("Post button clicked.")

        started_at = time.time()
        base_deadline = started_at + (POST_TIMEOUT_MS / 1000)
        progress_deadline = started_at + 180
        seen_progress_modal = False
        last_progress_log_at = started_at
        deadline = base_deadline

        while time.time() < deadline:
            skippable_warning = self.read_skippable_post_warning(page)
            if skippable_warning:
                self.logger.warn(
                    f"Post was blocked by a validation warning: {skippable_warning}"
                )
                return False, skippable_warning

            if re.search(r".*/drafts(?:[/?#].*)?$", page.url):
                page.wait_for_selector(TILE_SELECTOR)
                self.logger.info("Returned to the drafts page.")
                return True, ""

            if self.has_post_success_toast(page):
                self.logger.ok("Sora reported 'Video posted'. Returning to Drafts.")
                self.open_drafts_page(page)
                page.wait_for_selector(TILE_SELECTOR)
                self.logger.info("Returned to the drafts page.")
                return True, ""

            dialog = self.visible_dialog(page)
            if dialog is not None:
                dialog_text = self.read_dialog_text(dialog)
                if seen_progress_modal and self.is_drafts_ui_visible(page):
                    self.logger.info(
                        "Drafts appears to be loaded behind the post dialog. Recovering the Drafts view and continuing."
                    )
                    if self.recover_drafts_view_from_post_dialog(page, dialog):
                        page.wait_for_selector(TILE_SELECTOR)
                        self.logger.info("Returned to the drafts page.")
                        return True, ""
                if self.is_post_progress_dialog(dialog, dialog_text):
                    if not seen_progress_modal:
                        self.logger.info(
                            "Post opened a publish-progress dialog. Waiting for Sora to finish publishing."
                        )
                        seen_progress_modal = True
                        deadline = progress_deadline
                    elapsed = int(time.time() - started_at)
                    if time.time() - last_progress_log_at >= 15:
                        self.logger.info(
                            f"Still publishing... {elapsed}s elapsed. Waiting for Sora to return to Drafts."
                        )
                        last_progress_log_at = time.time()
                    if time.time() >= progress_deadline:
                        raise RuntimeError(
                            f"Publishing stayed in progress for too long ({elapsed}s) without returning to Drafts."
                        )
                    page.wait_for_timeout(1_000)
                    continue
                if seen_progress_modal and self.is_post_related_dialog(dialog_text):
                    elapsed = int(time.time() - started_at)
                    if time.time() - last_progress_log_at >= 15:
                        self.logger.info(
                            f"Still publishing... {elapsed}s elapsed. Waiting for the post dialog to clear."
                        )
                        last_progress_log_at = time.time()
                    if time.time() >= progress_deadline:
                        raise RuntimeError(
                            f"Publishing stayed in progress for too long ({elapsed}s) without returning to Drafts."
                        )
                    page.wait_for_timeout(1_000)
                    continue
                if self.is_skippable_post_dialog(dialog_text):
                    self.logger.warn(
                        f"Post was blocked by a validation dialog: {dialog_text or 'warning dialog'}"
                    )
                    if self.dismiss_dialog(dialog):
                        return False, dialog_text or "Post blocked by validation dialog."
                    raise RuntimeError(
                        f"Post was blocked by a dialog and the script could not dismiss it safely: {dialog_text or 'warning dialog'}"
                    )
                raise RuntimeError(
                    f"Unexpected modal appeared after clicking Post: {dialog_text or 'dialog with no readable text'}"
                )

            if seen_progress_modal and time.time() - last_progress_log_at >= 15:
                elapsed = int(time.time() - started_at)
                self.logger.info(
                    f"Still waiting for publish to finish... {elapsed}s elapsed. No Drafts page yet."
                )
                last_progress_log_at = time.time()
            page.wait_for_timeout(400)

        elapsed = int(time.time() - started_at)
        raise RuntimeError(f"Posting did not return to the drafts page in time after {elapsed}s.")

    def is_post_progress_dialog(self, dialog: Locator, dialog_text: str) -> bool:
        lowered = dialog_text.lower()
        if not self.is_post_related_dialog(dialog_text):
            return False

        try:
            post_button = dialog.get_by_role("button", name=re.compile(r"^Post$", re.I))
            if post_button.count():
                button = post_button.first
                if button.is_visible() and not button.is_enabled():
                    return True
        except PlaywrightError:
            pass

        try:
            if dialog.locator(".spin_loader").count():
                return True
        except PlaywrightError:
            pass

        return False

    def is_post_related_dialog(self, dialog_text: str) -> bool:
        lowered = dialog_text.lower()
        if "draft_" not in lowered:
            return False
        has_post_text = "post" in lowered
        has_edit_text = "edit" in lowered
        has_cast_text = "cast" in lowered
        return has_post_text and (has_cast_text or has_edit_text)

    def has_post_success_toast(self, page: Page) -> bool:
        try:
            toast = page.locator("[data-sonner-toast]").filter(
                has_text=re.compile(r"video posted", re.I)
            )
            return toast.count() > 0 and toast.first.is_visible()
        except PlaywrightError:
            return False

    def is_drafts_ui_visible(self, page: Page) -> bool:
        try:
            heading = page.get_by_role("heading", name=re.compile(r"^Drafts$", re.I))
            return heading.count() > 0 and heading.first.is_visible() and page.locator(TILE_SELECTOR).count() > 0
        except PlaywrightError:
            return False

    def recover_drafts_view_from_post_dialog(self, page: Page, dialog: Locator) -> bool:
        if not self.is_drafts_ui_visible(page):
            return False

        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(700)
        except PlaywrightError:
            pass

        if self.is_drafts_ui_visible(page):
            active_dialog = self.visible_dialog(page)
            if active_dialog is None or not self.is_post_related_dialog(self.read_dialog_text(active_dialog)):
                return True

        drafts_link = page.locator("a[href='/drafts']").first
        try:
            if drafts_link.count() and drafts_link.is_visible():
                drafts_link.click()
                page.wait_for_timeout(1_000)
        except PlaywrightError:
            pass

        return self.is_drafts_ui_visible(page)

    def visible_dialog(self, page: Page) -> Locator | None:
        dialog = page.locator("[role='dialog']").filter(has_text=re.compile(r".", re.S))
        count = dialog.count()
        for index in range(count):
            candidate = dialog.nth(index)
            try:
                if candidate.is_visible():
                    return candidate
            except PlaywrightError:
                continue
        return None

    def read_dialog_text(self, dialog: Locator) -> str:
        try:
            return " ".join(dialog.inner_text().split())
        except PlaywrightError:
            return ""

    def is_skippable_post_dialog(self, dialog_text: str) -> bool:
        lowered = dialog_text.lower()
        return any(pattern in lowered for pattern in SKIPPABLE_POST_PATTERNS)

    def read_skippable_post_warning(self, page: Page) -> str:
        try:
            toast = page.locator("[data-sonner-toast]").filter(has_text=re.compile(r".", re.S))
            count = toast.count()
            for index in range(count):
                candidate = toast.nth(index)
                if not candidate.is_visible():
                    continue
                text = " ".join(candidate.inner_text().split())
                if self.is_skippable_post_dialog(text):
                    return text
        except PlaywrightError:
            pass

        dialog = self.visible_dialog(page)
        if dialog is None:
            return ""

        text = self.read_dialog_text(dialog)
        if self.is_skippable_post_dialog(text):
            return text
        return ""

    def dismiss_skippable_post_warning(self, page: Page) -> bool:
        dialog = self.visible_dialog(page)
        if dialog is not None and self.dismiss_dialog(dialog):
            return True

        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(600)
            if not self.read_skippable_post_warning(page):
                self.logger.info("Dismissed the warning popup with Escape.")
                return True
        except PlaywrightError:
            pass

        try:
            page.mouse.click(20, 20)
            page.wait_for_timeout(600)
            if not self.read_skippable_post_warning(page):
                self.logger.info("Dismissed the warning popup by clicking away.")
                return True
        except PlaywrightError:
            pass

        return False

    def dismiss_dialog(self, dialog: Locator) -> bool:
        button_patterns = (
            re.compile(r"^(close|cancel|ok|okay|got it|dismiss)$", re.I),
            re.compile(r"^(close|cancel)$", re.I),
        )
        for pattern in button_patterns:
            buttons = dialog.get_by_role("button", name=pattern)
            count = buttons.count()
            for index in range(count):
                button = buttons.nth(index)
                try:
                    if button.is_visible() and button.is_enabled():
                        button.click()
                        self.logger.info("Dismissed the warning dialog.")
                        return True
                except PlaywrightError:
                    continue

        close_controls = dialog.locator(
            "button[aria-label*='close' i], button[title*='close' i], [data-state='open'] button"
        )
        count = close_controls.count()
        for index in range(count):
            button = close_controls.nth(index)
            try:
                if button.is_visible() and button.is_enabled():
                    button.click()
                    self.logger.info("Dismissed the warning dialog.")
                    return True
            except PlaywrightError:
                continue

        return False

    def find_icon_button(self, page: Page, path_fragment: str) -> Locator:
        candidate = page.locator("button").filter(
            has=page.locator(f"svg path[d*='{path_fragment}']")
        )
        count = candidate.count()
        for index in range(count):
            button = candidate.nth(index)
            try:
                if button.is_visible():
                    return button
            except PlaywrightError:
                continue
        raise RuntimeError(f"Unable to find the expected icon button: {path_fragment}")

    def capture_artifacts(
        self,
        page: Page,
        *,
        prefix: str,
        include_html: bool,
    ) -> tuple[Path, Path | None]:
        safe_prefix = re.sub(r"[^a-zA-Z0-9_-]+", "_", prefix).strip("_") or "artifact"
        stamp = make_slug()
        screenshot_path = self.runtime_paths.screenshots_dir / f"{safe_prefix}_{stamp}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)

        html_path = None
        if include_html:
            html_path = self.runtime_paths.html_dir / f"{safe_prefix}_{stamp}.html"
            html_path.write_text(page.content(), encoding="utf-8")

        return screenshot_path, html_path

    def write_checkpoint(self, checkpoint: Checkpoint) -> None:
        payload = asdict(checkpoint)
        self.runtime_paths.checkpoint_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )

    def load_checkpoint(self) -> Checkpoint | None:
        if not self.runtime_paths.checkpoint_file.exists():
            return None
        payload = json.loads(self.runtime_paths.checkpoint_file.read_text(encoding="utf-8"))
        if "skipped_draft_urls" not in payload:
            payload["skipped_draft_urls"] = []
        return Checkpoint(**payload)

    def full_url(self, href: str) -> str:
        if href.startswith("http://") or href.startswith("https://"):
            return href
        return f"{self.base_url}{href}"

    def extract_draft_id(self, draft_url: str) -> str:
        parsed = urlparse(draft_url)
        path = parsed.path.rstrip("/")
        if "/d/" in path:
            return path.rsplit("/", 1)[-1]
        return path.rsplit("/", 1)[-1]


def validate_positive(name: str, value: int) -> int:
    if value <= 0:
        raise typer.BadParameter(f"{name} must be greater than zero.")
    return value


@app.command()
def main(
    start_number: int = typer.Option(80, help="First numeric suffix to generate."),
    max_posts: int = typer.Option(3, help="Maximum drafts to process in this run."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Open and inspect drafts without saving or posting.",
    ),
    rename_only: bool = typer.Option(
        False,
        "--rename-only",
        help="Rename and save drafts, but do not post them.",
    ),
    keep_existing_title: bool = typer.Option(
        False,
        "--keep-existing-title",
        help="Do not rename drafts. Keep each draft's current title and post it as-is.",
    ),
    headful: bool = typer.Option(
        True,
        "--headful/--headless",
        help="Run with a visible browser window.",
    ),
    slow_mo: int = typer.Option(
        250,
        help="Delay between browser actions in milliseconds.",
    ),
    browser_channel: str = typer.Option(
        "chrome",
        help="Browser channel to launch with Playwright. Common values are chrome, msedge, or chromium.",
    ),
    browser_executable_path: str | None = typer.Option(
        None,
        help="Optional full browser executable path. Use this to force a specific Chrome binary.",
    ),
    cdp_url: str | None = typer.Option(
        None,
        help="Optional DevTools URL for attaching to an already-open Chrome session, such as http://127.0.0.1:9222.",
    ),
    launch_browser: bool = typer.Option(
        False,
        "--launch-browser",
        help="Launch the selected browser detached in remote-debugging mode and then attach over CDP.",
    ),
    user_data_dir: Path = typer.Option(
        ...,
        help="Browser profile directory used for the authorized session.",
        exists=False,
        file_okay=False,
        dir_okay=True,
        resolve_path=False,
    ),
    base_url: str = typer.Option(
        DEFAULT_BASE_URL,
        help="Base Sora URL. Override only if your session uses a different hostname.",
    ),
    resume_from_checkpoint: bool = typer.Option(
        False,
        "--resume-from-checkpoint",
        help="Continue from state/checkpoint.json when it exists.",
    ),
    manual_ready: bool = typer.Option(
        False,
        "--manual-ready",
        help="Wait for you to manually complete security/login and open Sora Drafts before automation starts.",
    ),
    auto_start_number: bool = typer.Option(
        False,
        "--auto-start-number",
        help="Scan visible page text for existing draft_000000-style titles and automatically move the start number forward when a safer next value is visible.",
    ),
    screenshot_on_success: bool = typer.Option(
        False,
        "--screenshot-on-success",
        help="Save a screenshot after each successful draft.",
    ),
) -> None:
    validate_positive("start-number", start_number)
    validate_positive("max-posts", max_posts)
    if slow_mo < 0:
        raise typer.BadParameter("slow-mo must be zero or greater.")
    if browser_channel not in {"chrome", "msedge", "chromium"}:
        raise typer.BadParameter("browser-channel must be one of: chrome, msedge, chromium.")
    if cdp_url and not cdp_url.startswith(("http://", "https://", "ws://", "wss://")):
        raise typer.BadParameter("cdp-url must start with http://, https://, ws://, or wss://.")
    if launch_browser and cdp_url and not cdp_url.startswith(("http://", "https://")):
        raise typer.BadParameter("launch-browser currently requires an http:// or https:// CDP URL.")
    if dry_run and rename_only:
        raise typer.BadParameter("Choose either --dry-run or --rename-only, not both.")
    if rename_only and keep_existing_title:
        raise typer.BadParameter(
            "--rename-only cannot be combined with --keep-existing-title because that would make no changes."
        )

    runtime_paths = RuntimePaths.create(REPO_ROOT)
    logger = RunLogger(runtime_paths, make_slug())
    poster = SoraPoster(
        base_url=base_url,
        start_number=start_number,
        max_posts=max_posts,
        dry_run=dry_run,
        rename_only=rename_only,
        keep_existing_title=keep_existing_title,
        headful=headful,
        slow_mo=slow_mo,
        browser_channel=browser_channel,
        browser_executable_path=browser_executable_path,
        cdp_url=cdp_url,
        launch_browser=launch_browser,
        user_data_dir=ensure_absolute(user_data_dir),
        screenshot_on_success=screenshot_on_success,
        resume_from_checkpoint=resume_from_checkpoint,
        manual_ready=manual_ready,
        auto_start_number=auto_start_number,
        runtime_paths=runtime_paths,
        logger=logger,
    )

    try:
        raise SystemExit(poster.run())
    except KeyboardInterrupt:
        logger.stop("Interrupted by user.")
        raise SystemExit(130) from None
    except Exception as exc:
        logger.error(str(exc))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    app()
