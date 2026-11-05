"""Search orchestration: authorization lives at the API boundary, SQL before embeddings."""

import math
from datetime import UTC, datetime
from threading import RLock

from pydantic import ValidationError

from app.config import Settings
from app.db import AuditRepository
from app.embeddings import EmbeddingAdapter, validate_vectors
from app.errors import AppError, invalid_output
from app.i18n import ACTION_TRANSLATIONS
from app.llm import LLMAdapter
from app.safety import safe_reason
from app.schemas import AuditRecord, SearchFilters, SearchResponse, SummaryItem, SummaryPlan

_ACTION = {
    "login": "кіру",
    "logout": "шығу",
    "password_change": "құпиясөзді өзгерту",
    "record_view": "жазбаны қарау",
    "record_update": "жазбаны өзгерту",
    "export": "экспорт",
    "grade_change": "бағаны өзгерту",
    "assignment_change": "пән бекітілімін өзгерту",
    "catalog_create": "оқу каталогына қосу",
    "catalog_update": "оқу каталогын өзгерту",
    "catalog_archive": "оқу каталогын мұрағаттау",
    "document_upload": "құжат жүктеу",
    "document_view": "құжат қарау",
    "document_delete": "құжат жою",
    "user_register": "пайдаланушыны тіркеу",
    "user_change": "пайдаланушыны өзгерту",
    "role_change": "рөлді өзгерту",
    "permission_change": "құқықтарды өзгерту",
    "session_block": "сессияны бұғаттау",
    "session_unblock": "сессия бұғатын алу",
    "session_terminate": "сессияны тоқтату",
    "session_flagged": "сессияны ереже бойынша белгілеу",
    "session_expired": "сессия мерзімінің аяқталуы",
    "access_denied": "қолжетімділікті қабылдамау",
    "admin_bootstrap": "алғашқы әкімшіні құру",
    "user_approve": "өтінімді мақұлдау",
    "user_reject": "өтінімді қабылдамау",
    "user_needs_review": "өтінімді қосымша тексеруге қалдыру",
    "student_claim_update": "студент мәліметін қайта ұсыну",
    "student_create": "студент жазбасын құру",
    "student_link": "студент аккаунтын байланыстыру",
    "task_create": "тапсырма құру",
    "task_update": "тапсырманы өзгерту",
    "task_material_upload": "тапсырма материалын жүктеу",
    "task_material_view": "тапсырма материалын қарау",
    "submission_submit": "жұмыс тапсыру",
    "submission_resubmit": "жұмысты қайта тапсыру",
    "submission_view": "жұмысты қарау",
    "submission_file_view": "жұмыс файлын қарау",
    "submission_review": "жұмыс бағасы мен пікірін өзгерту",
    "session_dismiss": "жалған дабыл шешімі",
    "session_ai_review": "AI тексеру ұсынысы",
}
_RESULT = {"success": "сәтті", "failure": "сәтсіз", "denied": "рұқсат берілмеді"}


def render_summary(
    plan: SummaryPlan, records: list[AuditRecord], locale: str = "kk"
) -> list[SummaryItem]:
    indexed = {record.audit_id: record for record in records}
    if not plan.claims:
        raise invalid_output()
    items: list[SummaryItem] = []
    rendered: set[tuple[str, int]] = set()
    for claim in plan.claims:
        ids = claim.audit_ids
        if len(set(ids)) != len(ids) or any(audit_id not in indexed for audit_id in ids):
            raise invalid_output()
        if claim.kind == "count":
            text = {
                "kk": f"Көрсетілген дәлелдерде {len(ids)} аудит жазбасы бар.",
                "ru": f"В выбранных доказательствах {len(ids)} записей аудита.",
                "en": f"The selected evidence contains {len(ids)} audit records.",
            }[locale]
            items.append(SummaryItem(text=text, kind=claim.kind, audit_ids=ids))
            continue

        for audit_id in ids:
            key = (claim.kind, audit_id)
            if key in rendered:
                continue
            rendered.add(key)
            record = indexed[audit_id]
            if claim.kind == "event":
                time = record.occurred_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
                text = f"{time}: {_ACTION[record.action]}; нәтиже — {_RESULT[record.result]}."
                if locale in {"ru", "en"}:
                    action = ACTION_TRANSLATIONS[record.action][0 if locale == "ru" else 1]
                    result = {
                        "ru": {"success": "успех", "failure": "неудача", "denied": "отказ"},
                        "en": {"success": "success", "failure": "failure", "denied": "denied"},
                    }
                    text = f"{time}: {action}; {result[locale][record.result]}."
            else:
                text = "Журналда тіркелген reason (дерек ретінде): «" + record.reason + "»."
                if locale == "ru":
                    text = "Записанная причина (данные журнала): «" + record.reason + "»."
                elif locale == "en":
                    text = "Recorded reason (journal data): “" + record.reason + "”."
            items.append(SummaryItem(text=text, kind=claim.kind, audit_ids=[audit_id]))
    return items


class AuditSearchService:
    def __init__(
        self,
        repository: AuditRepository,
        llm: LLMAdapter,
        embedder: EmbeddingAdapter,
        settings: Settings,
    ) -> None:
        self.repository = repository
        self.llm = llm
        self.embedder = embedder
        self.settings = settings
        self._reason_vectors: dict[str, list[float]] = {}
        self._cache_lock = RLock()

    def _semantic(self, records: list[AuditRecord], query: str) -> list[AuditRecord]:
        try:
            with self._cache_lock:
                missing = list(
                    dict.fromkeys(r.reason for r in records if r.reason not in self._reason_vectors)
                )
                if missing:
                    vectors = self.embedder.encode(missing)
                    validate_vectors(vectors, len(missing))
                    self._reason_vectors.update(zip(missing, vectors, strict=True))
                query_vectors = self.embedder.encode([query])
                validate_vectors(query_vectors, 1)
                query_vector = query_vectors[0]
                query_norm = math.sqrt(sum(value * value for value in query_vector))
                matches: list[AuditRecord] = []
                for record in records:
                    vector = self._reason_vectors[record.reason]
                    if len(vector) != len(query_vector):
                        raise ValueError("Mismatched embedding dimensions")
                    denominator = query_norm * math.sqrt(sum(value * value for value in vector))
                    score = (
                        sum(a * b for a, b in zip(query_vector, vector, strict=True)) / denominator
                    )
                    if not math.isfinite(score):
                        raise ValueError("Invalid similarity score")
                    score = max(-1.0, min(1.0, score))
                    if score >= self.settings.semantic_threshold:
                        matches.append(record.model_copy(update={"score": round(score, 6)}))
                return sorted(
                    matches,
                    key=lambda record: record.score if record.score is not None else 0.0,
                    reverse=True,
                )
        except AppError:
            raise
        except (ValueError, TypeError, AttributeError, OverflowError, RuntimeError) as exc:
            raise AppError(
                "model_unavailable", "Embedding моделі дұрыс вектор қайтармады.", 503
            ) from exc

    def search(self, question: str, locale: str = "kk") -> SearchResponse:
        try:
            parsed = self.llm.parse(question, datetime.now(UTC))
            # Revalidate at the trust boundary even if an adapter returned a model instance.
            filters = SearchFilters.model_validate_json(parsed.model_dump_json(), strict=True)
        except (ValidationError, ValueError, TypeError, AttributeError) as exc:
            raise invalid_output() from exc
        exact = (
            filters.start_at,
            filters.end_at,
            filters.action,
            filters.actions,
            filters.result,
            filters.user_login,
            filters.ip_address,
            filters.object_type,
            filters.object_id,
        )
        if not any(value is not None for value in exact) and filters.semantic_query is None:
            raise AppError(
                "unclear_question",
                "Сұрақтан аудит сүзгісі анықталмады. "
                "Уақытты, әрекетті немесе reason тақырыбын нақтылаңыз.",
                422,
            )
        candidates, capped = self.repository.search(
            filters, max_candidates=self.settings.max_candidates
        )
        records = [
            record.model_copy(update={"reason": safe_reason(record.reason)})
            for record in candidates
        ]
        threshold = self.settings.semantic_threshold if filters.semantic_query else None
        if records and filters.semantic_query:
            records = self._semantic(records, filters.semantic_query)
        matched_count = len(records)
        truncated = capped or matched_count > filters.limit
        records = records[: filters.limit]
        if not records:
            return SearchResponse(
                status="no_results",
                filters=filters,
                records=[],
                summary=[],
                insufficient_data=True,
                candidate_count=len(candidates),
                truncated=truncated,
                semantic_threshold=threshold,
            )
        try:
            raw_plan = self.llm.summarize(records)
            plan = SummaryPlan.model_validate_json(raw_plan.model_dump_json(), strict=True)
        except (ValidationError, ValueError, TypeError, AttributeError) as exc:
            raise invalid_output() from exc
        return SearchResponse(
            status="ok",
            filters=filters,
            records=records,
            evidence=self.repository.evidence([record.audit_id for record in records]),
            summary=render_summary(plan, records, locale),
            # Log facts cannot prove real causes/risk or absence outside returned records.
            insufficient_data=True,
            candidate_count=len(candidates),
            truncated=truncated,
            semantic_threshold=threshold,
        )
