ADEEB AI MEETING AGENT v15.5.2
STAGED TRANSCRIPTION + PROJECT FOLLOW-UPS + URDU ACCURACY
========================================================

QUICK START
1. Extract the complete ZIP to C:\AdeebAI or another short writable folder.
2. Do not copy an old .venv, .env, database, or data folder into this build during the first test.
3. Run FIRST_TIME_SETUP.bat once.
4. Open .env and set at minimum:
   ADMIN_PASSWORD=your-strong-private-password
   GROQ_API_KEY=your-valid-groq-key
5. Keep these recommended values on a slower computer:
   GROQ_STT_ENABLED=true
   GROQ_STT_ALL_LANGUAGES=true
   URDU_ASR_PROVIDER=auto
   STAGED_INTERVIEW_FLOW=true
   FAST_ANSWER_QUEUE=true
   PRELOAD_LOCAL_WHISPER=false
6. Run CHECK_SETUP.bat.
7. Run START_ADEEB.bat.
8. Keep the Local Server and Cloudflare Tunnel windows open.
9. Use only the newest /join link in CURRENT_PUBLIC_LINK.txt.

NEW INTERVIEW FLOW
1. Question 1: full introduction — saved and transcribed in the backend.
2. Question 2: applying role — saved and transcribed in the backend.
3. Question 3: skills/tools/work — saved and transcribed in the backend.
4. Adeeb waits for the skills transcript, then the LLM creates question 4 as a relevant skill/problem-solving follow-up.
5. Question 4 is saved and transcribed in the backend.
6. Adeeb waits for the first four transcripts, then the LLM creates question 5 as a role-specific project question.
7. Question 5 is transcribed immediately.
8. Adeeb asks exactly two immediate LLM follow-ups:
   - project depth and personal contribution
   - skill evidence, result, verification, or learning
9. The three project-section responses are stored together under question 5 for HR review.

LANGUAGE AND URDU
- Urdu remains locked until the candidate explicitly asks to change language.
- Urdu speech uses Groq multilingual Whisper when a Groq key is configured.
- Native Urdu script and English meaning are both retained.
- Roman Urdu output is not intentionally generated.
- Short spoken commands use the immediate path so talk in Urdu, talk in English, repeat, and next question still work.

DEPENDENCY SAFETY
- requirements.txt contains the direct runtime libraries.
- requirements-lock-py310.txt through requirements-lock-py313.txt contain the complete pinned dependency graph.
- The Windows installer automatically selects the lock for Python 3.10, 3.11, 3.12, or 3.13.
- Run REPAIR_ADEEB.bat if a virtual environment is incomplete or copied from another computer.

PRIVATE DATA
Each candidate can have a readable CNIC folder under data\candidates\<CNIC>\.
Do not share .env, data, logs, recordings, databases, or CURRENT_PUBLIC_LINK.txt.

Read COMPLETE_SYSTEM_REQUIREMENTS.txt before installing on another computer.
