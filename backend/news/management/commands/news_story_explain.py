"""Explain one Article's Story processing from recorded evidence (#33).

The explanation reads what the matcher recorded when it decided; it never
runs candidate retrieval again.
"""

from django.core.management.base import BaseCommand

from news.application.story_inspection import explain_article
from news.application.story_processing import reprocess_article

from ._operator import resolve_article, stamp, story_step_line


def _number(value) -> str:
    return "-" if value is None else f"{value:.6f}"


class Command(BaseCommand):
    help = (
        "Explain why an Article joined or created its Story, using the candidate "
        "evidence recorded at decision time, and show its processing state."
    )

    def add_arguments(self, parser):
        parser.add_argument("--article", required=True, help="Article id.")
        parser.add_argument(
            "--reprocess",
            action="store_true",
            help=(
                "Rebuild this Article's derived Story state through the reprocessing "
                "service first, then explain the new decision."
            ),
        )

    def handle(self, *args, **options):
        article_id = resolve_article(options["article"])
        if options["reprocess"]:
            self.stdout.write(story_step_line(reprocess_article(article_id)))
        explanation = explain_article(article_id)
        write = self.stdout.write

        processing = explanation.processing
        if processing is None:
            write(f"article_id={article_id} processing=MISSING (never attempted)")
        else:
            write(
                f"article_id={article_id} processing_state={processing.state} "
                f"attempts={processing.attempts} error_kind={processing.error_kind or '-'} "
                f"failed_step={explanation.failed_step or '-'} "
                f"retries_exhausted={'yes' if explanation.retries_exhausted else 'no'} "
                f"fresh={'yes' if explanation.fresh else 'no'} "
                f"updated_at={stamp(processing.updated_at)}"
            )
            write(
                f"embedding_model_key={processing.embedding_model_key or '-'} "
                f"matcher_key={processing.matcher_key or '-'}"
            )

        association = explanation.association
        if association is None:
            write("association=none")
            write(f"explanation: {explanation.summary}")
            return
        write(
            f"story_id={association.story_id} story_status={explanation.story_status} "
            f"method={association.method} is_primary={'yes' if association.is_primary else 'no'} "
            f"associated_at={stamp(association.associated_at)} "
            f"matcher_key={association.matcher_key or '-'}"
        )
        write(
            f"decision={explanation.decision} reason={explanation.reason or '-'} "
            f"match_rule={explanation.rule or '-'} "
            f"kept_current_story={'yes' if explanation.kept_current_story else 'no'} "
            f"chosen_story_id={association.story_id if explanation.decision == 'MATCH' else '-'} "
            f"distance={_number(explanation.distance)} threshold={explanation.threshold} "
            f"secondary_threshold={explanation.secondary_threshold} "
            f"secondary_member_threshold={explanation.secondary_member_threshold} "
            f"max_time_gap_hours={explanation.max_time_gap_hours} "
            f"candidate_count={explanation.candidate_count}"
        )
        write(f"evidence_embedding_model_key={explanation.embedding_model_key or '-'}")
        write(f"recorded_candidates={len(explanation.candidates)}")
        for rank, candidate in enumerate(explanation.candidates, start=1):
            within = (
                explanation.threshold is not None and candidate.distance <= explanation.threshold
            )
            write(
                f"  #{rank} story_id={candidate.story_id} distance={candidate.distance:.6f} "
                f"within_threshold={'yes' if within else 'no'}"
            )
        write(f"secondary_verifications={len(explanation.verifications)}")
        for check in explanation.verifications:
            write(
                f"  story_id={check.story_id} distance={check.distance:.6f} "
                f"result={check.result} member_distance={_number(check.member_distance)} "
                f"members_checked={check.members_checked} shared_anchors={check.shared_anchors}"
            )
        write(f"explanation: {explanation.summary}")
