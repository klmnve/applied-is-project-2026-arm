# Variant 19 · Department administrator workspace

[Қазақша](README.md) · [Русский](README.ru.md) · [English](README.en.md)

An educational access-management and audit system built with FastAPI, SQLite, HTML/CSS and plain JavaScript. Registration, server sessions, RBAC, assignments, grading, documents and audit work without AI. Local Ollama and a multilingual embedding model power the optional audit assistant.

The entire interface switches between Kazakh, Russian and English. The language choice on the sign-in page and top bar persists in localStorage; `Intl` formats dates and numbers. User names, submission text and original audit reasons remain data in their author's language.

The seed contains 3 groups, 12 fictional students, 3 subjects, 2 terms, 72 grades and 40 sample audit events. Names were invented for this educational dataset and do not describe real people; coincidental matches are possible. There is no connection to a university system. No accounts with known passwords are seeded.

## Install and run

Python 3.11+ is required. Run every command from the repository root. Reuse an existing working `.venv` instead of recreating it. For a fresh installation in Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m app.seed
.\.venv\Scripts\python.exe -m app.bootstrap --login YOUR_LOGIN --name "YOUR_DISPLAY_NAME"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Replace `YOUR_LOGIN` and `YOUR_DISPLAY_NAME` with your own values. Bootstrap prompts twice for a hidden password in an interactive terminal: 12–128 characters. Do not put the password in command arguments, `.env` or documentation. If an initial administrator exists, bootstrap neither creates another nor resets a password. An existing administrator assigns any additional administrators.

Open **http://127.0.0.1:8000** and sign in with the account you created. Stop the server with `Ctrl+C`. Select another supported Python version in the first command if necessary.

On Linux/macOS, run `python3 -m venv .venv`, then `. .venv/bin/activate`; use `python` instead of the Windows executable path and `cp .env.example .env` instead of `Copy-Item`. The remaining commands are the same.

Startup also initializes the schema and academic seed. Repeating `python -m app.seed` creates no duplicates and does not reset grades, edited names, accounts or passwords. The earlier `python -m app.db` audit seed command remains available.

## Registration, approval and roles

1. Register by requesting Teacher or Student. The requested `requested_role` is stored separately from the assigned `role_code`. Registration cannot request Administrator, Department head or Auditor.
2. Correct credentials allow an immediate restricted sign-in, exposing only your request status and sign-out. Protected APIs return `403`. Pending or rejected approval is not reported as an incorrect password.
3. An administrator reviews Registration requests and approves or rejects with a reason. Teacher requests approve only as Teacher; any later role change is a separate action in Users. Approval, rejection and role changes are audited.
4. For student approval, manually choose an existing record or add one from reviewed details after confirming its group. Each student record links to exactly one account. Students cannot change this link; administrator relinking is audited. Teachers then receive an exact group, subject and term through Teachers → Teacher profile → Add teaching assignment.

Permissions are loaded from the database on every protected request. Hiding buttons is not authorization. The former `X-Demo-User` authorization mechanism has been removed.

Student registration separately collects the student's full name, student ID/record-book number and study group. The number is trimmed and normalized to uppercase; only 3–32 ASCII letters/digits/`_`/`-` are accepted. These are **unverified claim details**, separate from the account display name and the confirmed `Students` link. Teacher forms hide and disable these fields and omit them from the payload. Public `GET /api/auth/registration-options` exposes only groups and email requirements, never the student roster.

The requests list shows a masked student number. Full numbers and email appear only on the owner's private status page and the administrator's `GET /api/admin/requests/{id}` review dialog. The administrator reviews the declared name, code and group, then manually chooses an existing record or confirms creation in the reviewed group; no student, group or confirmation checkbox is preselected. Approval requires `confirm_student_match=true`. For an existing-record link, a mismatch or missing legacy claim requires a separate `acknowledge_mismatch=true`; uncertainty can instead be returned for further review. The server also requires a nonempty, current `claim_revision` from the private detail response for every student approval: omission returns 422, and a changed claim returns 409 and must be reopened and checked again.

`POST /api/admin/requests/{id}/needs-review` records a reason and keeps the account pending, inactive and unlinked. The owner can read their own reason and use Correct request details (`PUT /api/auth/student-claim`) to resubmit safely. Approved students cannot use this route to change their claim or student link. Approval saves the role, activation and selected `student_id` atomically; a second account for an already linked student returns `409`. Existing `SYN-001..012` codes remain valid. Passwords, full unverified claim numbers and email are excluded from general audit text, server logs and general request lists. Authorized staff can see the stored official student code in the student roster.

The approval dialog offers **Choose an existing student** by default and a separate **Add a student from reviewed details** option. For creation, the administrator manually selects the actual catalog group and explicitly confirms the reviewed name, code and group; no group or checkbox is preselected. The UI sends `create_student=true`, `confirmed_group_id`, `confirm_student_match=true` and a fresh `claim_revision`, omitting `student_id`. The server reuses an exact existing record. A conflicting code always returns `409 student_selection_required` for manual selection. When only the full name matches and the code differs, the administrator must compare existing records and separately verify a different person before creating with `confirm_distinct_student=true`. This unchecked confirmation resets when the group or method changes and cannot override a conflicting code. The official `Students` record and account link are saved in one transaction. A pending claim does not create an official roster entry.

**Students** shows official `Students` records with their name, code and catalog group. All groups, group selection and name/code search combine in server requests to `GET /api/students?group_id=…&q=…&page=1&page_size=25`; blank `group_id` is omitted. Access requires the actual admin, head or teacher role and `Grades.View` or `Users.Manage`. Teachers see only assigned groups; students and auditors cannot access the roster. Empty catalogs, no matches and server errors have distinct states. Registration and correction collapse name whitespace and validate 2–120 characters; an empty group catalog blocks student submission. Teachers do not need student fields.

| Role | Default access |
|---|---|
| Administrator | Account, role, session, audit and academic management |
| Teacher | Students in assigned groups, assigned gradebooks, documents, assignments and submission review |
| Student | Published assignments for their group, their own submissions and published results |
| Department head | Student roster, academic and audit viewing without grade editing |
| Auditor | Dashboard and audit, without academic mutations |
| Pending / rejected | Sign-in, own request status and sign-out only |

`Learning.View`, `Learning.Manage` and `Learning.Submit` supplement Dashboard/Users/Roles/Grades/Documents/Sessions/Audit permissions. Subject, group and term scope is checked separately. Students cannot gain administrative or audit access through accidental permission grants. The last active administrator cannot be disabled, demoted or stripped of essential management access.

## Teacher approval and teaching workload

1. Teacher registration stays pending and inactive and requires no student claim fields. In **Teachers → Teacher requests**, the administrator searches, opens the profile and approves or rejects with a short reason. Every approval entry point grants a teacher request only `teacher`; no role selector is offered. A later role change is a separate action in general Users. General Users remains available for system account and role administration.
2. Add missing groups, subjects and terms in **Groups and subjects**. A term requires an academic year. Entries can be renamed, archived and reactivated; there is no delete API. Archiving preserves existing grade/task/audit links while blocking new assignments and tasks using that entry.
3. Open **Teachers → Teacher list → Teacher profile → Add teaching assignment** and select one exact group, subject and term together. Add multiple rows as needed; an identical tuple is not duplicated. Editing uses atomic `PATCH /api/academic/assignments/{assignment_id}`; revocation reuses `PUT /api/academic/assignments`. The profile and underlying list reload after changes.
4. **My teaching workload** on the teacher dashboard shows live exact tuples. The task form selects one complete assignment rather than independent lists. Without assignments, task creation is hidden and academic pages explain how to ask the department administrator for an assignment. Archiving a referenced catalog or revoking an assignment changes access in the current session too.
5. Deactivating a teacher ends active sessions. Reactivation never revives old tokens. Rejecting a teacher request through any entry point also terminates its pending sessions; a fresh login exposes only the owner’s rejected request status.

Server filtering/pagination uses `GET /api/admin/teachers` with view, status, search, group and subject. Profiles use `GET /api/admin/teachers/{id}` with dedicated approval/rejection/activation endpoints. Catalogs use `GET/POST/PATCH /api/admin/catalogs…`. Both admin pages require the actual admin role and `Users.Manage`. Own live workload uses `GET /api/academic/workload`. Assignment/catalog changes and audit entries share transactions; live permission and exact tuple checks use the database on each request.

## Assignments, submissions and grading

Documents → Upload stores a general academic document; it creates no assignment or deadline. Use Assignments → Create assignment for student work. Selecting an initial material in this form sends `POST /api/tasks/with-material`: the file, task and audits succeed together. Invalid files, revoked scope or audit failures leave no published task or new file. JSON `POST /api/tasks` remains available for assignments without material, and material can also be added later.

New and changed deadlines must be in the future according to the server clock (`422 deadline_in_past`). An already closed assignment can retain its deadline while its title, rules or late-submission setting changes. Untouched date inputs preserve their original UTC seconds and microseconds. Client-supplied `now`, `student_id` or timestamps cannot override server time or ownership.

A teacher creates an assignment within their scope, specifying a title, description, subject/group/term, publication time and required deadline. Submission rules and a safe material attachment are optional. Students see only published assignments for their group and their own work; changing API IDs cannot reveal another student's submission.

Submissions can be created and updated before the deadline. At `now >= deadline`, first submissions and updates are closed by default. Explicit `allow_late=true` permits both after the deadline and marks the submission as late. Deadlines are stored in UTC and displayed in the local time zone.

The assigned teacher enters an integer grade `0..100`, feedback and a separate publication choice. Unpublished grades, feedback and reviewer details are not sent to students. Resubmitting clears the previous review and publication, requiring the new version to be graded again. Version checks prevent overwriting concurrent edits. The assignment's subject/group/term cannot change once submissions exist.

Grade changes and history record the actor, UTC time, IP and previous/new values alongside audit in one transaction. Complete submission and feedback text are not copied into audit. AI does not grade work. Help reopens 2–5 concrete steps for the current role and page.

Exact old/new feedback, scores, publication states and versions are retained in `SubmissionReviewHistory`, together with actor, UTC, IP and audit ID. Review history in the UI and `GET /api/submissions/{id}/history` are restricted to authorized staff within scope; students cannot use it to obtain unpublished results. The general audit log keeps feedback digests and necessary score/version metadata, rather than private feedback bodies.

## Documents, sessions and audit

Documents and learning attachments support PDF, UTF-8 TXT, PNG and JPEG, up to **5 MiB** by default. Extension, MIME, content and size are checked. The server generates UUID filenames; user-supplied names never determine storage paths. Files are returned as attachments after authorization. Uploads, views, deletion and learning-file actions are audited. Document deletion is soft: the entry leaves the active list while its physical file remains.

The audit log records the actor, role at the time, UTC, IP, action, object, result and necessary previous/new business values. It supports date, actor, action, result and IP filters, pagination and details by audit ID. There is no audit edit/delete API; SQLite triggers also prohibit UPDATE/DELETE.

Explainable rules flag repeated failed sign-ins for a login/IP or a session IP change within a short interval. A reason, audit evidence and `awaiting_admin` state are recorded without automatic blocking. An administrator reviews the evidence and either blocks the session with a reason or records a false alarm. A fresh trigger after dismissal requires review again. Rate limiting remains independent.

AI can explain recorded evidence only, clearly labeled as a suggestion. It cannot establish a threat or decide to block a session. Blocking immediately invalidates the token. Unblocking moves the session to `revoked` and never restores the old token; the user signs in again. Disabling an account revokes its active sessions.

An AI-selected subset never replaces the underlying rule evidence: relevant failed-login records enter its safe projection, while original and concurrently added references remain available for human review. Decision reasons must still contain at least five meaningful characters after control-character sanitization.

## Security and configuration

Passwords use salted scrypt (`N=32768, r=8, p=3`). Random session tokens are issued in `HttpOnly`, `SameSite=Strict` cookies; only SHA-256 token and session-bound CSRF hashes are stored. Mutations check `X-CSRF-Token`, Origin and Fetch Metadata. Set `COOKIE_SECURE=true` behind HTTPS. Login errors do not reveal whether an account exists, and attempt limits persist in SQLite.

SQL uses bound parameters, the UI renders untrusted text with `textContent`, and CSP limits scripts and embedding. Passwords, cookies, tokens and embedding vectors never enter audit. Client IP comes from the socket peer by default. `X-Forwarded-For` is honored only for a specifically trusted immediate proxy, walking the chain from right to left.

| `.env` setting | Default |
|---|---|
| `DATABASE_PATH` / `UPLOAD_DIR` | `data/audit.db` / `data/uploads` |
| `MAX_UPLOAD_BYTES` / `SESSION_TTL_SECONDS` | `5242880` / `28800` |
| `COOKIE_SECURE` | `false` for localhost HTTP; use `true` for HTTPS |
| `STUDENT_EMAIL_REQUIRED` | `false`; enable to require an institution email for students |
| `STUDENT_EMAIL_DOMAIN` | empty; when set, the server enforces the email domain, for example `example.edu` |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_WINDOW_SECONDS` | `5` / `300` |
| `SUSPICIOUS_FAILURE_THRESHOLD` / `SUSPICIOUS_WINDOW_SECONDS` | `3` / `600` |
| `ALLOWED_ORIGINS` | `http://127.0.0.1:8000,http://localhost:8000` |
| `TRUSTED_PROXIES` | Empty; comma-separated IPs/CIDRs when needed |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | `http://localhost:11434` / `qwen2.5:1.5b` |
| `EMBEDDING_MODEL` / `EMBEDDING_PREFIX` | `models/multilingual-e5-small` / `query: ` |
| `SEMANTIC_THRESHOLD` / `LLM_TIMEOUT_SECONDS` / `MAX_CANDIDATES` | `0.80` / `180` / `500` |

Relative paths resolve from the process working directory, so start the server from the repository root. Restart after changing settings. Update `ALLOWED_ORIGINS` when using another host or port. Keep secrets out of `.env.example` and Git. Back up the SQLite database and upload directory together.

## Optional local AI models

First [install the Ollama application/CLI separately](https://ollama.com/download/windows); Python's `.[models]` extra does not install it. Then:

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[models]'
ollama serve
```

In another terminal:

```powershell
ollama pull qwen2.5:1.5b
.\.venv\Scripts\python.exe scripts/download_embedding.py --model intfloat/multilingual-e5-small --output models/multilingual-e5-small
```

Reuse existing models rather than downloading them again. Initial setup downloads files; inference runs locally without API keys or external LLM/embedding APIs. Embeddings use `local_files_only=True`, `trust_remote_code=False`. Ollama permits loopback origins only, with redirects and proxy environment settings disabled. Existing generation options remain `num_predict=512`, `num_ctx=4096`.

The closed AI schema accepts only validated dates, actions, result, login, IP, object, semantic query and limit. Unknown fields and invalid JSON are rejected; the backend alone builds parameterized SQL. Examples: `login: teacher.one`, `IP: 203.0.113.17`, `object: grade/17`, `action: submission_review`. Embeddings compare only safe reason text from SQL-prefiltered records. IP and old/new fields never enter model context. Every summary claim references returned audit IDs; journal text, including prompt injection, remains data.

Selecting a suggested question immediately starts one real search; example and submit buttons stay disabled while it runs. Malformed explicit login/IP/action/object criteria return `422 invalid_search_filter` instead of being silently dropped. Sentence-ending dots are punctuation; quote an identifier that intentionally ends with a dot, such as `login: "teacher.one."`.

E5 applies `query: ` to both compared texts. The `0.80` threshold is a configurable similarity cutoff, not a risk probability. “Last week” means the preceding 7×24 hours from UTC now; “today” starts at UTC midnight. The interval `[start_at,end_at)` excludes its end. Unavailable models produce a clear AI error while other modules continue working.

## Migration and verification

The additive, repeatable student-claim migration keeps `PRAGMA user_version=3`. Private `StudentClaims` stores `user_id`, `student_claim_name`, `student_claim_number`, `student_claim_group_id` and `student_claim_email`; `Users.request_review_required` marks further review. Existing accounts and academic records remain intact. Legacy missing claims are not populated by guessing a student's identity; unapproved student requests without details are marked for review.

Transactional v1/v2→v3 migration preserves original audit IDs/fields, accounts, grades and user edits. Existing approval states and pending requests remain distinct. Only exact old placeholder names for `SYN-001..012` are replaced; edited names remain untouched. Repeating migration/seed is safe and does not shift historical audit times.

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check app tests scripts
.\.venv\Scripts\python.exe -m ruff format --check app tests scripts
.\.venv\Scripts\python.exe -m mypy app scripts
.\.venv\Scripts\python.exe scripts/browser_smoke.py
.\.venv\Scripts\python.exe scripts/browser_smoke.py --student-claims-only
.\.venv\Scripts\python.exe scripts/browser_smoke.py --student-claims-only --require-student-email
.\.venv\Scripts\python.exe scripts/browser_smoke.py --student-roster-only
.\.venv\Scripts\python.exe scripts/browser_smoke.py --teachers-only
```

Tests use isolated databases, random runtime passwords and explicitly injected mock AI adapters; there is no production fallback. Browser smoke runs in isolation, saves artifacts to `artifacts/browser-smoke/` and creates no accounts in the regular database. If Chrome/Edge is unavailable, install a browser with `python -m playwright install chromium`. For stale temporary-directory ACL restrictions, use a new `--basetemp=.tools/pytest-new-run` without deleting working data. See the [main README](README.md#тексеру) for current full verification results.

Previous audit pass (2026-10-15): baseline **273 passed**; one complete final run **333 passed** (909.57 seconds), **60 added cases**, zero failures/errors/skips. Ruff check and formatting passed (45 files), as did MyPy for `app scripts` (24 files), JavaScript syntax and dependency checks. Chrome verified 42 automatic AI examples, all roles, approval/rejection/unique student links, UTC deadline persistence and cutoff, atomic task material, private review history and the final UI regressions, with zero JavaScript errors. Real local models verified both cited audit search and session-review suggestions without automatic blocking. The update preserved every existing database row and upload. All nine requested requirement groups now have evidence-backed complete status; see [requirements-audit.json](artifacts/requirements-audit.json), [final JUnit](artifacts/audit-final.xml) and the [Kazakh verification table](README.md#тексеру).

API schema: `/openapi.json`. `/api/health` checks process liveness; `/api/readiness` provides AI diagnostics protected by `Audit.View`. Modules are grouped into `store/db/seed`, `auth/auth_routes/bootstrap/network`, `academic`, `learning`, `core`, isolated AI code and `static/locales.json` + `i18n.js` + `app/i18n.py`.

Student-registration verification on 2026-10-15: **419 passed**, including all previous 333 tests and 86 added cases; no failures/errors/skips. Ruff check and formatting for `app tests scripts` passed (47 files), MyPy for `app scripts` passed (26 files), JavaScript syntax and 606 translated messages passed. Actual Chrome ran the full smoke plus the focused student flow in all three languages with optional and required institutional email: correction, further review, rejection, explicit matching, stale revision409, live-session approval, approved claim403, masking and audit/log privacy all passed with zero JavaScript errors. Existing database rows/uploads were preserved. [Full audit with actual APIs, schemas and changed files](artifacts/student-registration-audit.json), [JUnit](artifacts/student-claims-full.xml), [preservation report](artifacts/student-claim-upgrade.json). Whole-workspace `ruff check .` additionally reports four findings in the unrelated `lab6_tcp` folder; those user files were preserved.

Student-roster browser verification (2026-10-15): real Chrome `--student-roster-only` exited **0** in kk/ru/en. It checked pending claims absent from the roster, collapsed name whitespace, manual group confirmation, atomic student/account links, mandatory distinct-person confirmation for homonyms, server group+search filtering and pagination. Nine staff/locale checks, student/auditor API403, filter reset across accounts and recovery after assignment revocation passed. There were **0 JavaScript errors**, no page overflow at 390×844, and the closed navigation drawer stayed outside the viewport. [Result](artifacts/browser-smoke/student-roster-result.json), [mobile view](artifacts/browser-smoke/student-roster-mobile.png). Both focused claim modes (optional and required institutional email) were rerun and exited **0** across all three languages, preserving logout DOM clearing, revision/mismatch, correction and privacy regressions. The full main/AI smoke was not rerun for this roster change.

Teacher-flow browser verification (2026-10-15): real Chrome `--teachers-only` exited **0** in kk/ru/en. It verified pending registration, forced Teacher approval, catalog create/rename/archive/restore, multiple exact assignments without duplication, atomic edit/revocation, the exact task selector, forged cross-product403, live workload changes and recovery from revoked grade filters. Deactivation/rejection revoked sessions, and reactivation did not revive old tokens. [Result](artifacts/browser-smoke/teachers-result.json), [teacher list](artifacts/browser-smoke/teachers-en.png), [own workload](artifacts/browser-smoke/teacher-workload-en.png), [mobile](artifacts/browser-smoke/teachers-mobile.png).

The full main smoke also exited **0**: **13 pages**, **42** automatic AI examples with actual unavailable-model responses, **30** head/auditor views, existing file/deadline/submission/published-grade/history flows using the exact selector, sessions and mobile keyboard checks. A fresh direct `#documents` session verified the task shortcut. The roster and optional/required-email claim fixtures passed again. Every browser result reports **0 JavaScript errors**; this pass performed no real-model inference. [Main result](artifacts/browser-smoke/result.json). Scoped Ruff/format passed (**56 files**), MyPy passed (**31 files**), JavaScript syntax passed, and all **698** messages have three complete translations.

This is a local educational system. University integration, MFA/password recovery, malware scanning, external immutable audit storage, encrypted backups and retention policy are not implemented. SQLite triggers do not defend against a database-file administrator. Network deployment requires appropriate HTTPS/proxy configuration. AI never replaces authorization or a human decision.

<!-- student-roster-verification -->

### Student roster API verification · 2026-10-15

Separate runs passed: **419 regression**, **29 new approval**, and **32 roster/API cases**: **480 distinct cases**, zero failures/errors/skips. The 64 migration/redaction checks were additionally repeated and are not counted twice. Scoped Ruff/format (51 files), MyPy (28 files), both JS syntax checks and 637 translated messages passed. No schema or permission-matrix change was needed; repeated initialization/seed preserved every pre-existing row and upload. [Regression JUnit](artifacts/student-roster-regression.xml), [approval](artifacts/tests/student-roster-approval.xml), [roster](artifacts/students-roster.xml), [preservation/backup](artifacts/student-roster-preservation.json), [API, changed files and verification report](artifacts/student-roster-audit.json).

<!-- teacher-management-verification -->

### Teacher and catalog verification · 2026-10-15

Separate final runs passed **480 regression**, **31 new teacher** and **63 catalog/migration cases**: **574 distinct scenarios**, zero failures/errors/skips. The repeated 63 legacy migration cases are not counted twice. Scoped Ruff/format (56 files), MyPy (31 files), both JS syntax checks and 698 translated messages passed. Chrome verified the teacher/catalog, main learning and student workflows. Existing data/uploads were preserved. [Full API/file/results report](artifacts/teacher-management-audit.json), [regression](artifacts/teacher-regression.xml), [teachers](artifacts/tests/teacher-management-final.xml), [catalogs](artifacts/catalog-management.xml), [backup](artifacts/teacher-preservation.json).

<!-- /teacher-management-verification -->
