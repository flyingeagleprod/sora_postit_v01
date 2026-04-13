# Sora Post-It

Cross-platform `Python + Playwright` automation for overwriting Sora draft prompt text with numbered names and posting through your own authorized browser session.

## Windows Quick Start

If you are on Windows, open `PowerShell` and paste this exactly:

```powershell
cd "$HOME\Documents\github\sora_postit_v01"
& "$HOME\miniconda3\Scripts\conda.exe" create -n sora-postit python=3.11 -y
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit pip install -r requirements.txt
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit python -m playwright install
& "$HOME\miniconda3\envs\sora-postit\python.exe" -m sora_postit --launch-browser --cdp-url http://127.0.0.1:9222 --browser-executable-path "C:\Program Files\Google\Chrome\Application\chrome.exe" --manual-ready --dry-run --max-posts 1 --start-number 80 --user-data-dir .\chrome-debug-sora
```

What happens next:

1. A real Chrome window should open using the `.\chrome-debug-sora` folder.
2. The script launches Chrome detached and then waits. Your terminal stays usable.
3. If that Chrome debug session is already running and still logged into Sora, the script will reuse it instead of opening a fresh browser.
4. If a logged-in Sora session is already available, the script can skip the extra manual confirmation step and continue straight to Drafts.
5. In that Chrome window, manually go to Sora, complete the security check, and log in however you normally use Sora.
6. Google sign-in, OpenAI sign-in, passwordless sign-in, or another normal Sora-supported login flow are all fine.
7. After login, it is okay to leave the browser on any clearly logged-in Sora page such as `/profile`, `/drafts`, or another normal in-app page.
8. If the session is not already ready, go back to the terminal and press `Enter` after Sora is fully logged in.
9. The script will open `/drafts` for you after login is confirmed.
10. You do not need to scroll all the way to the bottom of Drafts by hand. The script is designed to do the scrolling and waiting itself once automation starts.
11. After the dry run works, come back to this README and use the `--rename-only` and live-run examples.
12. If PowerShell prints a `profile.ps1` signing warning when it opens, you can usually ignore it and keep going. That warning is separate from this project.

## What It Does

- Opens `/drafts`
- Scrolls to the bottom of the virtualized draft grid
- Remembers the last confirmed bottom `data-index` to speed up later bottom reacquisition in the same run or a resumed run
- Opens the bottom-most remaining draft
- Clicks the edit pencil
- Replaces Sora's single editable prompt text field with values like `draft_000080`
- Or, with `--keep-existing-title`, preserves the current prompt text and posts the draft as-is
- Clicks the checkmark save button
- Clicks `Post`
- If `Post` is blocked by a recognizable validation warning dialog, including missing or deleted cameo / mentioned-user cases, it skips that draft and continues
- Returns to `/drafts` and repeats

It also supports `--dry-run`, `--rename-only`, relative repo paths for logs and checkpointing, and screenshot/HTML capture on failure.
It also writes `prompt_archive.json` in the repo root, appending each draft's original full prompt text, assigned replacement text such as `draft_000080`, and stable draft ID from the draft URL before the prompt field is overwritten.

If visible page text already contains titles like `draft_000081`, the tool can warn that your requested `--start-number` looks behind. Add `--auto-start-number` if you want it to automatically move forward to the next visible safe number. The tool now also checks the latest visible Profile post before Drafts when it can, and the opened draft detail page performs its own numbered-title safety check before editing.

If no visible numbered titles can be found on Profile, Drafts, or the opened draft detail page, the tool does not guess `draft_000001`. It simply uses the `--start-number` you gave it.

## Project Paths

These are created relative to the repo root so the tool stays portable on Windows, macOS, and Linux:

- `logs/`
- `logs/screenshots/`
- `logs/html/`
- `state/checkpoint.json`
  This also remembers draft URLs that were skipped because they could not be posted, so later runs do not keep retrying the same blocked drafts.
- `prompt_archive.json`
  This stores the original prompt text before overwrite, the assigned replacement text, and the draft ID from the Sora draft URL so prompts are backed up as the run proceeds.

## Requirements

- Python 3.11 in a conda environment named `sora-postit`
- Playwright browser binaries installed with `python -m playwright install`
- A browser profile directory passed with `--user-data-dir`
- A real installed Chrome browser is recommended for Sora.

## Where To Run This

Run this on the same computer where you normally use Sora in a browser.

- On Windows, open `PowerShell`
- On macOS, open `Terminal`
- On Linux, open your normal shell or terminal

Then change into this project folder before running any setup or automation commands.

Windows example:

```powershell
cd "$HOME\Documents\github\sora_postit_v01"
```

macOS example:

```bash
cd /path/to/sora_postit_v01
```

If you are not inside the project folder first, commands like `pip install -r requirements.txt` and `python -m sora_postit ...` will not run correctly.

## Windows Setup

These commands assume Miniconda is installed at `$HOME\miniconda3`.

If `conda activate ...` does not work in your PowerShell window, that is okay. You do not need activation for this project. Use the full `conda.exe` path shown below.

Create the environment:

```powershell
& "$HOME\miniconda3\Scripts\conda.exe" create -n sora-postit python=3.11 -y
```

Install dependencies:

```powershell
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit pip install -r requirements.txt
```

Install Playwright browsers:

```powershell
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit python -m playwright install
```

Optional help check:

```powershell
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit python -m sora_postit --help
```

## macOS Setup

Create the environment:

```bash
conda create -n sora-postit python=3.11 -y
conda activate sora-postit
```

Install dependencies and Playwright browsers:

```bash
pip install -r requirements.txt
python -m playwright install
```

Optional help check:

```bash
python -m sora_postit --help
```

## Browser Profile Notes

Do not hardcode a browser path in the code. Pass one when you run the tool.

A dedicated automation profile is usually safer than pointing at a live Chrome profile that is already open in another window. A simple cross-platform choice is a repo-local profile folder:

- Windows: `.\chrome-debug-sora`
- macOS: `./chrome-debug-sora`

On the first run, open the browser window, log in to Sora in that profile, then reuse the same `--user-data-dir` on later runs.

If Sora or Cloudflare seems unhappy with Playwright-launched browsers, use Chrome attach mode. It launches real Chrome detached with remote debugging and then attaches the automation to it.

## CDP URL Notes

- `127.0.0.1` means "this same computer." It is the local loopback address on Windows, macOS, and Linux.
- `9222` is just the chosen Chrome remote-debugging port in the examples. It is not special to one machine or operating system.
- If `9222` is already in use on someone else's computer, they can choose another free local port such as `9223`, as long as the browser launch command and the `--cdp-url` flag use the same port.
- The browser executable path is the machine-specific part, not the `127.0.0.1` address. Windows, macOS, and Linux users may need different Chrome paths.
- For users who do not want to think about ports, the safest path is to follow the platform example exactly and only change the browser executable path and profile directory if needed.

## Universal Launch Template

This is the cross-platform shape of the command. Only the browser path, profile path, and optionally the start number usually need to change:

```bash
python -m sora_postit --launch-browser --cdp-url http://127.0.0.1:9222 --browser-executable-path "/path/to/browser" --manual-ready --auto-start-number --headful --max-posts 9999 --start-number 81 --slow-mo 250 --user-data-dir ./chrome-debug-sora --resume-from-checkpoint
```

Replace only these parts:

- `"/path/to/browser"`
- `./chrome-debug-sora`
- `81` if your numbering should start somewhere else

Common browser path examples:

- Windows Chrome: `"C:/Program Files/Google/Chrome/Application/chrome.exe"`
- macOS Chrome: `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"`
- Linux Chrome: `"/usr/bin/google-chrome"`

If a user already has the Chrome debug port running, this same command can still be used because the script will reuse the existing CDP session instead of launching a second browser.

## First-Time User Steps

Use this order the first time you set the tool up:

1. Open `PowerShell` on Windows or `Terminal` on macOS or Linux.
2. Change into the project folder.
3. Create or choose a dedicated browser profile folder, such as `.\chrome-debug-sora` on Windows or `./chrome-debug-sora` on macOS.
4. Run the tool in `--dry-run` mode with that profile path.
5. For the first run, start with `--launch-browser --cdp-url http://127.0.0.1:9222 --manual-ready` so the tool launches real Chrome detached and does not try to interact while Sora is performing security verification.
6. In the opened browser, manually go to Sora, complete the security check, and log in however you normally use Sora.
7. Google sign-in, OpenAI sign-in, passwordless sign-in, or another normal Sora-supported login flow are all fine.
8. After login, it is okay to leave the browser on `/profile`, `/drafts`, or another clearly logged-in Sora page.
9. On later runs, if that Chrome debug session is still open and still logged in, the tool should reuse it automatically.
10. If the session is already ready, the tool can continue without asking for `Enter`.
11. If login is not complete yet, return to the terminal and press `Enter` so the script can continue.
12. The script will open `/drafts` for you after it confirms the Sora session is logged in.
13. Leave the profile folder in place and keep reusing the same `--user-data-dir` on later runs so the saved session is reused.
14. You do not need to manually scroll all the way to the bottom before running the tool.
15. Let the script handle the long draft loading process. It is designed to keep scrolling down, pause, wait for more drafts to appear, and continue until it reaches the bottom of the virtualized grid.
16. If a draft cannot be posted because a mentioned or cameo user no longer exists, the tool marks that draft as skipped, remembers its draft URL in `state/checkpoint.json`, and continues upward to the next draft instead of retrying it forever.
17. After the first dry run succeeds, test `--rename-only --max-posts 1`.
18. Then test one real live item with `--max-posts 1`.
19. Only after that should you use the default first live batch size of `--max-posts 3`.

If the drafts page is especially slow, that is okay. The tool is meant to be patient during the scroll-to-bottom step rather than requiring you to preload the whole list manually.

## Chrome Attach Mode

If Sora is still getting stuck on Cloudflare when the tool launches the browser, use attach mode.

This is the recommended first-run command in Git Bash because it uses one terminal command and launches Chrome detached for you:

```bash
python -m sora_postit --launch-browser --cdp-url http://127.0.0.1:9222 --browser-executable-path "C:/Program Files/Google/Chrome/Application/chrome.exe" --manual-ready --dry-run --max-posts 1 --start-number 80 --user-data-dir ./chrome-debug-sora
```

What this does:

1. Launches real Chrome detached with remote debugging enabled
2. Uses `./chrome-debug-sora` as the Chrome profile for that debug session
3. Waits while you complete any security or human verification
4. Waits while you log in to Sora
5. Opens `/drafts` for you after you press `Enter` in the terminal

If you prefer to launch Chrome yourself and then attach later, that still works:

```bash
"/c/Program Files/Google/Chrome/Application/chrome.exe" --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-debug-sora"
```

Then attach with:

```bash
python -m sora_postit --cdp-url http://127.0.0.1:9222 --manual-ready --dry-run --max-posts 1 --start-number 80 --user-data-dir ./chrome-debug-sora
```

## First Commands To Paste

Windows first-time setup:

```powershell
cd "$HOME\Documents\github\sora_postit_v01"
& "$HOME\miniconda3\Scripts\conda.exe" create -n sora-postit python=3.11 -y
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit pip install -r requirements.txt
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit python -m playwright install
& "$HOME\miniconda3\envs\sora-postit\python.exe" -m sora_postit --launch-browser --cdp-url http://127.0.0.1:9222 --browser-executable-path "C:\Program Files\Google\Chrome\Application\chrome.exe" --manual-ready --dry-run --max-posts 1 --start-number 80 --user-data-dir .\chrome-debug-sora
```

macOS first-time setup example:

```bash
cd /path/to/sora_postit_v01
conda create -n sora-postit python=3.11 -y
conda activate sora-postit
pip install -r requirements.txt
python -m playwright install
python -m sora_postit --launch-browser --cdp-url http://127.0.0.1:9222 --browser-executable-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --manual-ready --dry-run --max-posts 1 --start-number 80 --user-data-dir ./chrome-debug-sora
```

## Example Commands

Dry run one draft:

```powershell
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit python -m sora_postit --launch-browser --cdp-url http://127.0.0.1:9222 --browser-executable-path "C:\Program Files\Google\Chrome\Application\chrome.exe" --manual-ready --dry-run --max-posts 1 --start-number 80 --user-data-dir .\chrome-debug-sora
```

Rename only:

```powershell
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit python -m sora_postit --launch-browser --cdp-url http://127.0.0.1:9222 --browser-executable-path "C:\Program Files\Google\Chrome\Application\chrome.exe" --manual-ready --rename-only --max-posts 1 --start-number 80 --user-data-dir .\chrome-debug-sora
```

Post without changing titles:

```powershell
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit python -m sora_postit --launch-browser --cdp-url http://127.0.0.1:9222 --browser-executable-path "C:\Program Files\Google\Chrome\Application\chrome.exe" --manual-ready --keep-existing-title --headful --max-posts 3 --user-data-dir .\chrome-debug-sora
```

First live batch:

```powershell
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit python -m sora_postit --launch-browser --cdp-url http://127.0.0.1:9222 --browser-executable-path "C:\Program Files\Google\Chrome\Application\chrome.exe" --manual-ready --headful --max-posts 3 --start-number 80 --slow-mo 250 --user-data-dir .\chrome-debug-sora --resume-from-checkpoint
```

Attach to an already-open Chrome:

```powershell
& "$HOME\miniconda3\Scripts\conda.exe" run -n sora-postit python -m sora_postit --cdp-url http://127.0.0.1:9222 --manual-ready --dry-run --max-posts 1 --start-number 80 --user-data-dir .\chrome-debug-sora
```

Equivalent macOS run:

```bash
python -m sora_postit --launch-browser --cdp-url http://127.0.0.1:9222 --browser-executable-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --manual-ready --headful --max-posts 3 --start-number 80 --slow-mo 250 --user-data-dir ./chrome-debug-sora --resume-from-checkpoint
```

## CLI Options

- `--start-number`
- `--max-posts`
- `--dry-run`
- `--rename-only`
- `--keep-existing-title`
- `--headful` or `--headless`
- `--slow-mo`
- `--browser-channel`
- `--browser-executable-path`
- `--cdp-url`
- `--launch-browser`
- `--user-data-dir`
- `--base-url`
- `--resume-from-checkpoint`
- `--manual-ready`
- `--screenshot-on-success`

## Safety Behavior

- Default live limit is `--max-posts 3`
- Stops on selector mismatch or unexpected state
- Writes JSONL and CSV logs for every processed draft
- Saves `state/checkpoint.json` after each successful item
- Captures screenshot and page HTML on failure

## Suggested Validation Order

1. `python -m sora_postit --help`
2. `--dry-run --max-posts 1`
3. `--rename-only --max-posts 1`
4. Full run with `--max-posts 1`
5. First live batch with `--max-posts 3`
