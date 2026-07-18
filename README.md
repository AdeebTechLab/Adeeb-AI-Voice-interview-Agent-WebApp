# Adeeb AI Voice Interview Agent WebApp

Adeeb AI Voice Interview Agent is a Windows-based web application for running AI-assisted voice interviews through a local browser interface. It creates a private Python environment, installs version-locked dependencies, starts the application on port `8000`, and can optionally create a temporary Cloudflare link for access from another device.

The project includes configurable interview questions, company knowledge, identity settings, speech processing, text-to-speech, document handling, an admin interface, setup checks, and repair tools.

## Main Features

- Browser-based interview and administration interface
- AI-assisted interview flow
- Voice transcription with Faster Whisper
- Text-to-speech support through Edge TTS
- Configurable questions in `questions.json`
- Configurable company context in `company_knowledge.md`
- Application identity settings in `identity.py`
- Local data storage inside the project
- Private Python virtual environment
- Version-locked dependencies for supported Python versions
- Automatic security-secret generation
- Local access at `http://127.0.0.1:8000`
- Optional Cloudflare public/mobile link
- Built-in setup, repair, and system-check scripts

## Technology

The project uses:

- Python 3.11, 3.12, or 3.13 (64-bit recommended)
- FastAPI
- Uvicorn
- Faster Whisper
- CTranslate2
- Edge TTS
- Jinja2
- HTTPX
- PyPDF
- Cryptography
- Cloudflared for optional external access

## System Requirements

Before setup, make sure the computer has:

- Windows 10 or Windows 11
- 64-bit Python 3.11, 3.12, or 3.13
- An internet connection for first-time dependency installation
- Enough free disk space for Python packages and speech models
- A modern browser such as Chrome, Edge, or Firefox

During Python installation, enable:

```text
Add python.exe to PATH
Install launcher for all users
```

Check Python with:

```powershell
python --version
```

or:

```powershell
py --version
```

## Project Structure

```text
Adeeb-AI-Voice-interview-Agent-WebApp/
├── app.py
├── identity.py
├── questions.json
├── company_knowledge.md
├── .env.example
├── .gitignore
├── requirements.txt
├── requirements-lock-py311.txt
├── requirements-lock-py312.txt
├── requirements-lock-py313.txt
├── FIRST_TIME_SETUP.bat
├── START_ADEEB.bat
├── CHECK_SETUP.bat
├── REPAIR_ADEEB.bat
├── INSTALL_CLOUDFLARE.bat
├── data/
├── logs/
├── scripts/
├── static/
└── templates/
```

## First-Time Installation

### 1. Extract the complete project

Extract the complete ZIP to a normal folder, for example:

```text
D:\Adeeb-AI-Voice-interview-Agent-WebApp
```

Do not run the application directly from inside the ZIP.

### 2. Run the setup

Double-click:

```text
FIRST_TIME_SETUP.bat
```

The setup will:

1. Validate the project files.
2. Create `.env` from `.env.example` when needed.
3. Detect a compatible 64-bit Python installation.
4. Create the private `.venv` environment.
5. Select the matching dependency lock file.
6. Install required Python packages.
7. Generate security secrets.
8. Download Cloudflared when possible.

### 3. Configure the environment

Open `.env` and set at least:

```env
ADMIN_PASSWORD=replace-with-a-strong-password
GROQ_API_KEY=replace-with-your-groq-api-key
```

Keep `.env` private. Never upload it to GitHub or share it publicly.

### 4. Start the application

Double-click:

```text
START_ADEEB.bat
```

After the health check passes, the local admin interface opens at:

```text
http://127.0.0.1:8000
```

If Cloudflared is available, the application also creates a temporary public link and saves it in:

```text
CURRENT_PUBLIC_LINK.txt
```

Keep the local server and Cloudflare tunnel windows open while interviews are running.

## Configuration

### Interview questions

Edit:

```text
questions.json
```

Keep the JSON syntax valid. A missing comma, extra comma, or unmatched quotation mark can prevent the application from loading the question set.

### Company knowledge

Edit:

```text
company_knowledge.md
```

Use this file for company background, services, policies, role information, or other context the AI should use during interviews.

### Application identity

Edit:

```text
identity.py
```

Use this file for supported branding and identity settings. Keep a backup before changing Python code.

### Environment settings

Local secrets and deployment settings belong in:

```text
.env
```

Use `.env.example` only as a safe template without real credentials.

## Utility Scripts

### Check the installation

Run:

```text
CHECK_SETUP.bat
```

It checks:

- `.env`
- System Python
- `.venv`
- Installed packages
- Python source syntax
- Cloudflared
- Server health

### Repair the installation

Run:

```text
REPAIR_ADEEB.bat
```

Repair removes and rebuilds only the private `.venv` environment. It is designed to preserve `.env` and candidate/application data.

Close all Adeeb and Python windows before running repair.

### Install Cloudflare support

Run:

```text
INSTALL_CLOUDFLARE.bat
```

Cloudflare support is optional. The local application can still run without it.

## Common Problems

### Dependency lock file was not found

Example:

```text
Dependency lock file was not found:
requirements-lock-py313.txt
```

The selected Python version requires its matching lock file. Extract the complete project ZIP again and confirm the correct file exists in the project root.

Do not create an empty lock file and do not rename a lock file from another Python version.

### Python is not recognized

Try:

```powershell
py --version
python --version
where.exe python
```

Install a supported 64-bit Python version and restart VS Code or PowerShell afterward.

### Port 8000 is already in use

Close the program currently using port `8000`, or restart Windows before running `START_ADEEB.bat` again.

To inspect the port:

```powershell
netstat -ano | findstr :8000
```

### Server does not become ready

Check:

```text
logs\server.log
logs\last_error.txt
```

Also run:

```text
CHECK_SETUP.bat
```

### Cloudflare tunnel DNS timeout

The local application may still be working even when the public tunnel fails. Confirm:

```text
http://127.0.0.1:8000
```

Then restart the Cloudflare tunnel and check the internet connection or DNS settings.

### GitHub blocks the push because of a secret

Never commit:

- `.env`
- `logs/`
- API keys
- service-account credentials
- private key files
- candidate data

A suitable `.gitignore` should contain:

```gitignore
.env
.env.*
!.env.example
logs/
*.log
.venv/
__pycache__/
data/
*.key
*.pem
```

If a credential was already committed, removing the file locally is not enough. Revoke or rotate the credential and remove it from Git history before pushing again.

## Security Notes

- Replace the default admin password before company use.
- Store API credentials only in `.env`.
- Do not expose the public Cloudflare link unnecessarily.
- Treat candidate recordings, transcripts, answers, and reports as private information.
- Back up required data before repair, migration, or manual code changes.
- Rotate any credential that appears in a log or Git commit.
- Do not publish `logs/server.log`.

## Updating Dependencies

The application uses Python-version-specific lock files:

```text
requirements-lock-py311.txt
requirements-lock-py312.txt
requirements-lock-py313.txt
```

Do not manually mix lock files between Python versions. Test dependency changes in a separate project copy before distributing an updated package.

## Local Development

Activate the private environment in PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

Check the environment:

```powershell
python --version
python -m pip --version
```

Deactivate it with:

```powershell
deactivate
```

For normal use, the provided `.bat` launchers are recommended because they perform validation and health checks automatically.

## Data and Backups

Before making major changes, back up:

```text
data/
.env
questions.json
company_knowledge.md
identity.py
```

Do not commit private production data to the repository.

## License

See the included [`LICENSE`](LICENSE) file for the project's licensing terms.

## Support and Diagnostics

When reporting a setup or startup problem, include:

- The exact command or batch file used
- A screenshot of the complete terminal error
- Python version
- Windows version
- Relevant non-secret lines from `logs/server.log`
- Contents of `logs/last_error.txt`

Remove API keys, passwords, tokens, candidate information, and private URLs before sharing logs.
