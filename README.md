# CBIT Proctored Coding Examination Portal

## 1. Interactive Browser Portal (`cbit_exam_portal.html`)
Open `cbit_exam_portal.html` directly in any browser (or use the interactive Canvas preview) to test:
- **CBIT Student Registration & Login**: Validates 12-digit CBIT roll numbers (`1601xxxxxxxx`) and institutional emails (`@cbit.ac.in`, `@cbit.org.in`).
- **Pre-Configured Accounts**:
  - **Admin**: `admin` / `admin123`
  - **Testing Student**: `160100000000` / `test123`
- **Multi-Language Code Editor & Compiler**: Write, run, and grade solutions in **Python 3, Java, and C** against visible sample test cases and hidden grading test cases.
- **Admin Question & Auto-Grading Manager**: Author questions, define sample & hidden test cases, and choose between **Partial (Proportional)**, **All-or-Nothing**, and **Weighted (30% Sample / 70% Hidden)** grading.
- **Fullscreen Lockdown & Proctoring Telemetry**: Blocks and flags copy/paste/cut, right-click, tab switching (`visibilitychange`), window blur (`blur`), keyboard shortcuts, and fullscreen exits, reporting all flags in real time to the Admin dashboard.

## 2. Local Full-Stack Flask Backend (`app.py`)
To run with native OS-level `python3`, `gcc`, and `javac`/`java` subprocess execution and SQLite persistence:

```bash
pip install flask werkzeug
python3 app.py
```
Then open `http://localhost:5000` in your browser.
