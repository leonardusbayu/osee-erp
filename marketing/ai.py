"""Marketing recommendations: local facts, optional constrained external ranking."""

import hashlib
import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.formats import date_format

from core.audit import record
from core.models import AccessThrottle, Membership, Organization
from director.ai import DirectorAIUnavailable, urlopen
from director.models import AIUsageCounter, AIUsageReservation
from director.templatetags.director_format import director_money

from .management import COMMERCIAL_FACTS, MANAGEMENT_FACTS, management_snapshot
from .models import MarketingAIPolicy

DISCLOSURE_VERSION = "marketing-aggregate-v2"
DISCLOSURE_TEXT = (
    "Hanya intent lokal, kode rekomendasi, dan metrik agregat marketing terbatas dapat dikirim ke OpenRouter "
    "serta endpoint yang disetujui. Nama anggota, pelanggan, perusahaan dan kampanye, pertanyaan, riwayat, "
    "referensi sumber, nomor invoice/bank, dan dokumen tidak dikirim. Agregat tetap rahasia. "
    "AI hanya mengurutkan rekomendasi yang dihitung aplikasi, tidak mengubah anggaran/iklan, "
    "menjalankan pembayaran, atau mengirim pesan. Dampak cash-in tidak dijamin. "
    "Jumlah KPI, kelengkapan sumber, evaluasi yang berubah, serta antrean bukti pekerjaan dapat disertakan; identitas dan bukti pekerjaan tetap lokal."
    " Agregat pesanan lunas, peserta, mitra, refund, pendapatan layanan dan biaya unit terverifikasi juga dapat disertakan; identitas pembeli dan dokumen tetap lokal."
)
MAX_RESPONSE_BYTES = 65536
METRIC_KEYS = {
    "cash_in",
    "actual_refunds",
    "net_cash_in",
    "spend",
    "leads",
    "cohort_paid_leads",
    "cohort_paid_conversion",
    "cohort_fully_paid_leads",
    "cohort_fully_paid_conversion",
    "followups_due",
    "followups_on_time",
    "followup_on_time_percent",
    "median_response_minutes",
    "followed_up_leads",
    "response_coverage_percent",
    "overdue_followups",
    "cash_to_spend",
    "unattributed_receipts",
    "total_invoice_refunds",
    "attributed_invoice_refunds",
    "unattributed_invoice_refunds",
    "attribution_percent",
    "spend_rows",
    "spend_known_rows",
    "unpaid_won_leads",
    "unpaid_won_amount",
}


class MarketingAIUnavailable(Exception):
    pass


def _decimal(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def build_recommendations(snapshot, question=""):
    """Pure local result, also used to turn an evidenced recommendation into a task."""
    summary, coverage = snapshot["summary"], snapshot["coverage"]
    period = snapshot["period"]
    period_start = date_format(date.fromisoformat(period["start"]), "j F Y")
    period_end = date_format(date.fromisoformat(period["end"]), "j F Y")
    recorded_at = timezone.localtime(
        datetime.fromisoformat(snapshot["as_of"]), ZoneInfo("Asia/Jakarta")
    )
    evidence = f"Periode {period_start} sampai {period_end}. Data ditinjau {date_format(recorded_at, 'j F Y, H:i')} WIB."
    recommendations = []

    def add(
        code,
        title,
        reason,
        action,
        priority,
        metric_key,
        value,
        success,
        owner="Manager marketing",
    ):
        recommendations.append(
            {
                "id": code,
                "title": title,
                "reason": reason,
                "action": action,
                "description": action,
                "priority": priority,
                "metric_key": metric_key,
                "metric_value": value,
                "evidence": f"{evidence} {reason}",
                "owner_hint": owner,
                "success_measure": success,
            }
        )

    unmatched = _decimal(coverage.get("unattributed_receipts"))
    if unmatched is not None and unmatched > 0:
        add(
            "link_receipts",
            "Hubungkan penerimaan dengan sumber lead",
            f"Uang masuk {director_money(unmatched)} belum terhubung dengan lead pada seluruh organisasi dalam periode ini.",
            "Tinjau invoice yang belum terkait dengan lead bersama finance. Pertahankan sumber yang belum diketahui sebagai belum diketahui; jangan menebak kampanye.",
            "high",
            "unattributed_receipts",
            str(unmatched),
            "Porsi kas dengan sumber terverifikasi naik; total kas tetap cocok dengan finance.",
            "Manager marketing dan finance",
        )
    unmatched_refunds = _decimal(coverage.get("unattributed_invoice_refunds"))
    if unmatched_refunds is not None and unmatched_refunds > 0:
        add(
            "link_invoice_refunds",
            "Periksa refund invoice yang belum terhubung",
            f"Refund invoice aktual {director_money(unmatched_refunds)} belum terhubung dengan lead pada seluruh organisasi dalam periode ini.",
            "Minta Finance memeriksa invoice dan lead sumber refund yang belum terkait. Pertahankan refund tersebut sebagai belum teratribusi sampai hubungan sumber diverifikasi.",
            "medium",
            "unattributed_invoice_refunds",
            str(unmatched_refunds),
            "Refund invoice aktual yang belum terhubung berkurang setelah bukti sumber ditinjau; total refund tetap cocok dengan Finance.",
            "Manager marketing dan finance",
        )
    overdue = summary.get("overdue_followups")
    if overdue:
        add(
            "follow_up",
            "Selesaikan follow-up yang terlambat",
            f"Ada {overdue} lead aktif dengan jadwal follow-up terlewat dalam lingkup filter.",
            "Bagi antrean ke penanggung jawab, periksa hambatan pembelian/pembayaran, dan tetapkan jadwal tindak lanjut baru. Hubungi pelanggan hanya setelah staf meninjau konteksnya.",
            "high",
            "overdue_followups",
            overdue,
            "Antrean terlambat berkurang; pantau pembayaran teralokasi dari lead yang ditindaklanjuti.",
            "Penanggung jawab lead",
        )
    due_promises, timely_promises = (
        summary.get("followups_due"),
        summary.get("followups_on_time"),
    )
    if due_promises and timely_promises is not None and timely_promises < due_promises:
        add(
            "review_followup_discipline",
            "Tinjau janji follow-up yang lewat tenggat",
            f"{timely_promises} dari {due_promises} janji yang jatuh tempo memiliki bukti penyelesaian tepat waktu pada periode ini.",
            "Periksa riwayat janji dan bukti kontak. Cari penyebab keterlambatan, bagi ulang antrean bila perlu, dan pastikan koreksi memiliki alasan serta tidak mengubah waktu yang sudah lewat.",
            "high",
            "followup_on_time_percent",
            summary.get("followup_on_time_percent"),
            "Bagian janji dengan bukti tercatat tepat waktu meningkat; sumber tetap dapat ditinjau.",
            "Koordinator sales",
        )
    if summary.get("unpaid_won_leads"):
        add(
            "collect_agreed",
            "Tinjau pembayaran yang belum lunas",
            f"Ada {summary['unpaid_won_leads']} lead sepakat membeli dengan sisa invoice {director_money(summary['unpaid_won_amount'])}. Sepakat membeli belum berarti pembayaran diterima.",
            "Periksa jatuh tempo dan hambatan pembayaran bersama finance. Tetapkan follow-up yang sesuai untuk sisa tagihan, lalu nilai hasil dari penerimaan teralokasi.",
            "high",
            "unpaid_won_amount",
            summary["unpaid_won_amount"],
            "Sisa tagihan yang ditinjau berkurang melalui penerimaan teralokasi yang valid.",
            "Penanggung jawab lead dan finance",
        )
    incomplete_detail = next(
        (
            item
            for item in snapshot.get("rows", [])
            if not item.get("product_mapping_complete", True)
            or not item.get("cash_cost_coverage_complete", True)
        ),
        None,
    )
    if not summary.get("spend_complete") or incomplete_detail:
        spend_reason = (
            summary.get("spend_note", "Belanja belum lengkap.")
            if not summary.get("spend_complete")
            else incomplete_detail["spend_note"]
        )
        add(
            "complete_spend",
            "Lengkapi dasar evaluasi belanja iklan",
            spend_reason,
            "Lengkapi laporan belanja beserta referensi sumber pada dimensi yang didukung. Gunakan kanal atau kampanye untuk membandingkan belanja dengan kas; jangan membagi belanja ke staf berdasarkan tebakan.",
            "high",
            "spend_known_rows",
            summary.get("spend_known_rows", 0),
            "Seluruh baris belanja periode diketahui dan tidak dihitung dua kali.",
            "Media buyer",
        )
    leads = summary.get("leads")
    if leads and summary.get("followed_up_leads", 0) < leads:
        add(
            "record_responses",
            "Tinjau lead tanpa waktu respons",
            f"Waktu respons tercatat untuk {summary.get('followed_up_leads') or 0} dari {leads} lead periode ini.",
            "Periksa apakah lead belum ditangani atau waktu respons belum dicatat. Perbaiki pembagian antrean dan pencatatan sebelum membandingkan kecepatan anggota.",
            "medium",
            "response_coverage_percent",
            summary.get("response_coverage_percent"),
            "Cakupan pencatatan respons membaik; lead yang belum ditangani mendapat penanggung jawab.",
            "Koordinator sales",
        )
    management = snapshot.get("management", {})
    management_counts = management.get("counts", {})
    if management_counts.get("kpi_sources_incomplete"):
        add(
            "complete_kpi_sources",
            "Periksa seluruh sumber sebelum menilai KPI",
            f"Ada {management_counts['kpi_sources_incomplete']} target aktif dalam seluruh organisasi yang belum memiliki sumber lengkap dan diperiksa.",
            "Buka ringkasan hasil marketing. Cocokkan daftar seluruh sumber dengan data masuk, duplikat, pengecualian, dan pemeriksaan bank bila ada ukuran pembayaran. Minta direktur mengesahkan dasar yang cocok.",
            "high",
            "kpi_sources_incomplete",
            management_counts["kpi_sources_incomplete"],
            "Target dinilai hanya setelah cakupan sumber cocok; nilai yang belum lengkap tetap sementara.",
        )
    if management_counts.get("kpi_stale_reviews"):
        add(
            "refresh_kpi_reviews",
            "Perbarui evaluasi yang dasar datanya berubah",
            f"Ada {management_counts['kpi_stale_reviews']} evaluasi target dengan data sumber yang berbeda dari saat ditinjau.",
            "Tinjau koreksi dan kelengkapan sumber, lalu simpan evaluasi baru tanpa mengganti riwayat pemeriksaan lama.",
            "high",
            "kpi_stale_reviews",
            management_counts["kpi_stale_reviews"],
            "Evaluasi baru menyebut sumber dan sidik data terbaru.",
        )
    if management_counts.get("work_waiting_review"):
        add(
            "review_work_evidence",
            "Periksa bukti pekerjaan yang diajukan",
            f"Ada {management_counts['work_waiting_review']} pekerjaan menunggu pemeriksa terpisah; pengajuan tertua {management_counts.get('work_review_oldest_days', 0)} hari.",
            "Direktur meninjau bukti versi terbaru, menerima hasil atau meminta revisi. Setelah diterima, nilai dampak bisnis melalui KPI terkait secara terpisah.",
            "high",
            "work_waiting_review",
            management_counts["work_waiting_review"],
            "Bukti ditinjau oleh pemeriksa berbeda; penerimaan pekerjaan tidak dianggap sebagai penjualan.",
            "Direktur",
        )
    commercial = management.get("commercial", {})
    if commercial.get("cost_pending_invoices"):
        add(
            "complete_unit_costs",
            "Lengkapi biaya aktual layanan sebelum menilai margin",
            f"Ada {commercial['cost_pending_invoices']} invoice layanan selesai dengan biaya unit belum lengkap. Selisih pendapatan dan biaya belum tersedia.",
            "Finance mencocokkan tagihan pemasok dan mengalokasikan biaya layanan yang selesai. Jangan memakai harga pemasok historis sebagai biaya aktual periode ini.",
            "high",
            "cost_pending_invoices",
            commercial["cost_pending_invoices"],
            "Biaya unit memiliki sumber dan alokasi yang lengkap sebelum digunakan dalam keputusan budget.",
            "Finance dan direktur",
        )
    if commercial.get("cohort_orders", 0) > commercial.get(
        "cohort_fully_paid_orders", 0
    ):
        add(
            "review_order_settlement",
            "Tinjau pesanan yang belum seluruhnya lunas",
            f"Dari {commercial['cohort_orders']} pesanan tercatat pada periode ini, {commercial['cohort_fully_paid_orders']} memiliki seluruh tagihan lunas hingga akhir pengamatan; pembayaran sebagian belum termasuk lunas.",
            "Periksa komponen invoice, pembayaran, kredit, dan pembatalan bersama Finance. Hubungkan rencana follow-up dengan peluang serta bukti pembayaran, bukan hanya status sepakat membeli.",
            "medium",
            "cohort_fully_paid_orders",
            commercial["cohort_fully_paid_orders"],
            "Komponen pesanan dan status pelunasannya dapat ditelusuri; kesimpulan hanya mencakup pesanan yang tercatat.",
        )
    response_misses = [
        row
        for row in management.get("targets", [])
        if row["evaluation"].get("official")
        and row["evaluation"].get("state") == "not_met"
        and row["evaluation"].get("unit") == "menit"
    ]
    if response_misses:
        add(
            "response_review",
            "Tinjau target respons yang belum tercapai",
            f"Ada {len(response_misses)} target median respons yang telah disetujui dan belum tercapai dengan sumber diperiksa.",
            "Buka target untuk melihat lingkup serta batas yang disepakati. Tinjau jam masuk dan mutu respons sebelum mengubah pembagian kerja; median ini memakai menit kalender, berbeda dari SLA jam kerja.",
            "medium",
            "kpi_not_met",
            len(response_misses),
            "Perbaikan diuji terhadap target yang disepakati dan cakupan respons lengkap.",
            "Koordinator sales",
        )
    if leads:
        paid = summary.get("cohort_paid_leads", 0)
        observed_end = date_format(
            date.fromisoformat(snapshot.get("observation_end", period["end"])), "j F Y"
        )
        add(
            "cohort_review",
            "Evaluasi hambatan sampai pembayaran",
            f"Dari {leads} lead yang masuk pada periode ini, {paid} telah memiliki pembayaran diterapkan ke invoice (termasuk uang muka) sampai {observed_end}.",
            "Tinjau lead berkualitas yang belum membayar menurut kampanye, produk, segmen, dan umur lead. Pisahkan hambatan penawaran, jadwal layanan, dan pembayaran; uji satu perbaikan dengan cohort pembanding.",
            "medium",
            "cohort_paid_conversion",
            summary.get("cohort_paid_conversion"),
            "Konversi pembayaran cohort dengan umur observasi sama membaik; hindari menyimpulkan dari cohort yang belum matang.",
        )
        fully_paid = summary.get("cohort_fully_paid_leads")
        if fully_paid is not None and paid is not None and paid > fully_paid:
            add(
                "review_partial_payments",
                "Pisahkan pembayaran sebagian dari penjualan lunas",
                f"{paid} prospek memiliki pembayaran, sedangkan {fully_paid} memiliki seluruh invoice aktif bernilai neto yang lunas penuh.",
                "Tinjau sisa tagihan, jatuh tempo, dan bukti pembayaran bersama finance. Pertahankan pembayaran sebagian sebagai status tersendiri; jangan laporkan sebagai penjualan lunas.",
                "medium",
                "cohort_fully_paid_conversion",
                summary.get("cohort_fully_paid_conversion"),
                "Konversi lunas penuh dan daftar piutang cocok dengan invoice neto serta penerapan pembayaran.",
                "Sales dan finance",
            )
    current_cash = _decimal(summary.get("cash_in"))
    previous_summary = snapshot.get("previous_summary")
    if not isinstance(previous_summary, dict):
        previous_summary = {}
    previous_cash = _decimal(previous_summary.get("cash_in"))
    if (
        current_cash is not None
        and previous_cash is not None
        and previous_cash > 0
        and current_cash < previous_cash
    ):
        current_net_cash = _decimal(summary.get("net_cash_in"))
        add(
            "cash_change",
            "Telusuri penurunan penerimaan periode",
            f"Kas bruto teratribusi {director_money(current_cash)}, dibanding {director_money(previous_cash)} pada periode sebelumnya dengan panjang sama. Kas setelah refund periode ini {director_money(current_net_cash) if current_net_cash is not None else 'belum tersedia'}.",
            "Pisahkan perubahan volume lead, kualitas lead, waktu pembayaran, dan kelengkapan rekonsiliasi. Periksa invoice yang belum dibayar bersama finance sebelum mengubah belanja.",
            "high",
            "cash_in",
            str(current_cash),
            "Penyebab penurunan terdokumentasi dan tindakan diuji terhadap penerimaan teralokasi berikutnya.",
        )
    if not summary.get("source_period_complete"):
        add(
            "confirm_period_coverage",
            "Konfirmasikan cakupan sumber sebelum menilai anggaran",
            summary.get(
                "source_coverage_note", "Cakupan sumber periode belum dikonfirmasi."
            ),
            "Periksa seluruh akun dan laporan sumber untuk kampanye serta periode yang dipilih. Catat referensi bukti dan total biaya sumber; jangan menebak bahwa hari kosong berarti biaya nol.",
            "high",
            "spend_known_rows",
            summary.get("spend_known_rows", 0),
            "Cakupan sumber sesuai periode dan angka sumber cocok dengan catatan yang ditinjau.",
            "Manager marketing",
        )
    if summary.get("cash_to_spend") is not None and summary.get(
        "source_period_complete"
    ):
        add(
            "controlled_test",
            "Rancang eksperimen iklan dengan hasil pembayaran",
            f"Rasio kas/belanja periode tercatat {summary['cash_to_spend']}×. Kas ini dapat berasal dari lead lama dan bukan ukuran laba atau sebab akibat iklan.",
            "Pilih kampanye untuk eksperimen terbatas dengan hipotesis, batas biaya yang disetujui manager, cohort pembanding, dan waktu evaluasi pembayaran. Nilai juga biaya layanan serta refund sebelum keputusan peningkatan belanja.",
            "medium",
            "cash_to_spend",
            summary["cash_to_spend"],
            "Bandingkan penerimaan cohort dengan usia sama dan biaya yang lengkap; jangan menjanjikan kenaikan cash-in.",
            "Manager marketing dan media buyer",
        )
    if not recommendations:
        add(
            "collect_evidence",
            "Bangun dasar analisis pembayaran",
            "Data dalam lingkup ini belum cukup untuk mengevaluasi hubungan marketing dan pembayaran.",
            "Catat lead, sumber, penanggung jawab, belanja iklan, dan hubungan invoice. Finance mencocokkan penerimaan; setelah itu evaluasi cohort pembayaran.",
            "high",
            "leads",
            leads,
            "Lead, belanja, dan pengaitan invoice memiliki bukti yang dapat ditinjau.",
        )
    intent = (
        "cash"
        if any(word in question.casefold() for word in ("cash", "kas", "bayar"))
        else "team"
        if any(
            word in question.casefold()
            for word in ("tim", "anggota", "respons", "follow")
        )
        else "ads"
        if any(word in question.casefold() for word in ("ads", "iklan", "kampanye"))
        else "overview"
    )
    preferred = {
        "cash": {"link_receipts", "cash_change", "cohort_review"},
        "team": {"follow_up", "record_responses", "response_review"},
        "ads": {"complete_spend", "controlled_test"},
    }.get(intent, set())
    recommendations.sort(
        key=lambda item: (item["id"] not in preferred, item["priority"] != "high")
    )
    return {
        "mode": "local",
        "mode_label": "Saran dari aturan aplikasi",
        "intent": intent,
        "formula_version": snapshot["formula_version"],
        "summary": "Prioritas berikut didasarkan pada data yang tersedia. Pilih tindakan, tetapkan penanggung jawab, lalu ukur hasil pembayaran.",
        "recommendations": recommendations,
        "notice": "Saran lokal berbasis aturan; belum menggunakan model AI. Angka dihitung aplikasi, dampak cash-in tidak dijamin.",
    }


def _membership(organization, actor, owner_only=False):
    allowed = (
        ("owner",) if owner_only else ("owner", "director", "finance", "marketing")
    )
    if (
        not getattr(actor, "is_authenticated", False)
        or not Membership.objects.filter(
            organization=organization,
            user=actor,
            user__is_active=True,
            role__in=allowed,
        ).exists()
    ):
        raise PermissionDenied


def _policy(organization, actor, version=None):
    _membership(organization, actor)
    policy = MarketingAIPolicy.objects.filter(
        organization=organization,
        enabled=True,
        approved_at__isnull=False,
        disclosure_version=DISCLOSURE_VERSION,
    ).first()
    if (
        policy is None
        or not Membership.objects.filter(
            organization=organization,
            user_id=policy.approved_by_id,
            role="owner",
            user__is_active=True,
        ).exists()
        or (version is not None and policy.policy_version != version)
    ):
        raise MarketingAIUnavailable
    return policy


def _config():
    if (
        getattr(settings, "MARKETING_AI_PRIVATE_AGGREGATES_ENABLED", False) is not True
        or getattr(settings, "DIRECTOR_AI_ENDPOINT_APPROVED", False) is not True
    ):
        raise MarketingAIUnavailable
    config = {
        key: getattr(settings, setting, "")
        for key, setting in (
            ("key", "DIRECTOR_OPENROUTER_API_KEY"),
            ("model", "DIRECTOR_OPENROUTER_MODEL"),
            ("provider", "DIRECTOR_OPENROUTER_PROVIDER"),
        )
    }
    if (
        not all(isinstance(value, str) and value.strip() for value in config.values())
        or any(
            not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,180}", config[key])
            for key in ("model", "provider")
        )
        or config["model"] in {"openrouter/auto", "openrouter/free"}
    ):
        raise MarketingAIUnavailable
    for key, setting in (
        ("ceiling", "DIRECTOR_AI_MAX_REQUEST_COST_USD"),
        ("budget", "DIRECTOR_AI_MONTHLY_BUDGET_USD"),
        ("user_budget", "DIRECTOR_AI_USER_MONTHLY_BUDGET_USD"),
    ):
        config[key] = _decimal(getattr(settings, setting, "0"))
        if (
            config[key] is None
            or config[key] <= 0
            or config[key] > Decimal("999999.999999")
            or config[key].as_tuple().exponent < -6
        ):
            raise MarketingAIUnavailable
    if config["ceiling"] > min(config["budget"], config["user_budget"]):
        raise MarketingAIUnavailable
    return config


def _safe_facts(snapshot):
    facts = {}
    management = snapshot.get("management")
    if not isinstance(management, dict):
        management = {}
    management_ai_facts = management.get("ai_facts")
    if not isinstance(management_ai_facts, dict):
        management_ai_facts = {}
    commercial_facts = management.get("commercial")
    if not isinstance(commercial_facts, dict):
        commercial_facts = {}
    for key in sorted(METRIC_KEYS):
        value = snapshot["summary"].get(key, snapshot["coverage"].get(key))
        number = _decimal(value)
        if number is not None and abs(number) < Decimal(100000000000000000000):
            facts[key] = format(number, "f")
    for key in MANAGEMENT_FACTS:
        value = management_ai_facts.get(key)
        if type(value) is int and 0 <= value < 1000000000:
            facts["management_" + key] = str(value)
    for key in COMMERCIAL_FACTS:
        value = commercial_facts.get(key)
        number = None if isinstance(value, bool) else _decimal(value)
        if number is not None and abs(number) < Decimal(100000000000000000000):
            facts["commercial_" + key] = format(number, "f")
    return facts


def _outbound_body(advice, snapshot, config):
    identifiers = [item["id"] for item in advice["recommendations"]]
    # No arbitrary labels, question, source strings or employee/customer identifiers cross this boundary.
    context = {
        "intent": advice["intent"],
        "facts": _safe_facts(snapshot),
        "recommendation_ids": identifiers,
        "basis": "gross_allocated_receipts_plus_actual_invoice_refunds_paid; net_cash_is_not_profit; cash_spend_ratio_is_not_causal",
    }
    body = {
        "model": config["model"],
        "provider": {
            "only": [config["provider"]],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        },
        "messages": [
            {
                "role": "system",
                "content": "Rank the supplied recommendation IDs using only supplied facts. Return each ID exactly once. Do not produce new numbers, prose, identities, tools, or actions. Gross receipts are not net cash, profit, or causal ad return.",
            },
            {"role": "user", "content": json.dumps(context)},
        ],
        "stream": False,
        "max_tokens": 350,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "marketing_priorities_v1",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "recommendation_ids": {
                            "type": "array",
                            "minItems": len(identifiers),
                            "maxItems": len(identifiers),
                            "items": {"type": "string", "enum": identifiers},
                        }
                    },
                    "required": ["recommendation_ids"],
                    "additionalProperties": False,
                },
            },
        },
    }
    encoded = json.dumps(body).encode("utf-8")
    if len(encoded) > 18000:
        raise MarketingAIUnavailable
    return encoded


def validate_completion(payload, advice):
    try:
        choices = payload["choices"]
        if (
            not isinstance(payload, dict)
            or "error" in payload
            or not isinstance(choices, list)
            or len(choices) != 1
        ):
            raise ValueError
        choice = choices[0]
        message = choice["message"]
        if (
            choice.get("finish_reason") != "stop"
            or message.get("refusal")
            or message.get("tool_calls")
        ):
            raise ValueError
        result = json.loads(message["content"])
        identifiers = result["recommendation_ids"]
        expected = {item["id"] for item in advice["recommendations"]}
        if (
            set(result) != {"recommendation_ids"}
            or not isinstance(identifiers, list)
            or any(not isinstance(item, str) for item in identifiers)
            or len(identifiers) != len(expected)
            or set(identifiers) != expected
        ):
            raise ValueError
        return identifiers
    except (KeyError, TypeError, ValueError, AttributeError):
        raise MarketingAIUnavailable from None


@transaction.atomic
def _reserve(organization, actor, config):
    Organization.objects.select_for_update().get(pk=organization.pk)
    policy = _policy(organization, actor)
    if min(policy.request_cap_usd, policy.monthly_budget_usd) < config["ceiling"]:
        raise MarketingAIUnavailable
    month = timezone.now().strftime("%Y-%m")
    limits = (
        (f"global:{month}", config["budget"]),
        (
            f"marketing:org:{organization.pk}:{month}",
            min(policy.monthly_budget_usd, config["budget"]),
        ),
        (f"user:{actor.pk}:{month}", config["user_budget"]),
    )
    for key, limit in limits:
        AIUsageCounter.objects.get_or_create(key=key)
        if not AIUsageCounter.objects.filter(
            key=key, reserved_usd__lte=limit - config["ceiling"]
        ).update(reserved_usd=F("reserved_usd") + config["ceiling"]):
            raise MarketingAIUnavailable
    reservation = AIUsageReservation.objects.create(
        organization=organization,
        actor=actor,
        request_id="marketing-" + uuid.uuid4().hex,
        amount_usd=config["ceiling"],
        status="reserved",
    )
    return reservation, policy.policy_version, [key for key, _ in limits]


@transaction.atomic
def _usage(reservation, status, actual_cost=None, counter_keys=()):
    Organization.objects.select_for_update().get(pk=reservation.organization_id)
    reservation = AIUsageReservation.objects.select_for_update().get(
        pk=reservation.pk, organization_id=reservation.organization_id
    )
    if status == "dispatching" and reservation.status != "reserved":
        raise MarketingAIUnavailable
    if reservation.status == "completed" and status == "outcome_unknown":
        return
    if actual_cost is not None and actual_cost > reservation.amount_usd:
        AIUsageCounter.objects.filter(key__in=counter_keys).update(
            reserved_usd=F("reserved_usd") + actual_cost - reservation.amount_usd
        )
        policy = MarketingAIPolicy.objects.select_for_update().get(
            organization_id=reservation.organization_id
        )
        policy.enabled = False
        policy.save(update_fields=["enabled"])
        status = "outcome_unknown"
    reservation.status = status
    if actual_cost is not None:
        reservation.actual_cost = actual_cost
    reservation.save(update_fields=["status", "actual_cost"])


@transaction.atomic
def _dispatch(reservation, organization, actor, version):
    Organization.objects.select_for_update().get(pk=organization.pk)
    _policy(organization, actor, version)
    _usage(reservation, "dispatching")


def _external(advice, snapshot, organization, actor):
    config = _config()
    body = _outbound_body(advice, snapshot, config)
    reservation, version, keys = _reserve(organization, actor, config)
    try:
        _dispatch(reservation, organization, actor, version)
        request = Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + config["key"],
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=20) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status != 200 or len(raw) > MAX_RESPONSE_BYTES:
                raise MarketingAIUnavailable
        payload = json.loads(raw)
        ranked = validate_completion(payload, advice)
        actual = None
        if (
            isinstance(payload.get("usage"), dict)
            and payload["usage"].get("cost") is not None
        ):
            actual = _decimal(payload["usage"]["cost"])
            if (
                actual is None
                or actual < 0
                or actual > Decimal("999999.999999")
                or actual.as_tuple().exponent < -6
            ):
                raise MarketingAIUnavailable
        _usage(reservation, "completed", actual, keys)
        if actual is not None and actual > reservation.amount_usd:
            raise MarketingAIUnavailable
        _policy(organization, actor, version)
        return ranked
    except (
        MarketingAIUnavailable,
        DirectorAIUnavailable,
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        ValueError,
    ):
        _usage(reservation, "outcome_unknown")
        raise MarketingAIUnavailable from None


@transaction.atomic
def _rate(organization, actor):
    minute = timezone.now().replace(second=0, microsecond=0)
    day = minute.replace(hour=0, minute=0)
    for identity, start, limit in (
        (f"marketing:user-minute:{actor.pk}:{minute.isoformat()}", minute, 6),
        (f"marketing:user-day:{actor.pk}:{day.isoformat()}", day, 80),
        (f"marketing:org-day:{organization.pk}:{day.isoformat()}", day, 250),
    ):
        key = hashlib.sha256(identity.encode()).hexdigest()
        AccessThrottle.objects.get_or_create(key=key, defaults={"window_start": start})
        if not AccessThrottle.objects.filter(key=key, attempts__lt=limit).update(
            attempts=F("attempts") + 1
        ):
            raise MarketingAIUnavailable


def marketing_advice(*, organization, actor, snapshot, question=""):
    _membership(organization, actor)
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("organization_id") != organization.pk
    ):
        raise ValidationError("Snapshot tidak sesuai organisasi.")
    if not isinstance(question, str) or len(question) > 2000 or "\x00" in question:
        raise ValidationError("Pertanyaan maksimal dua ribu karakter.")
    snapshot = {
        **snapshot,
        "management": management_snapshot(
            organization=organization,
            start=snapshot["period"]["start"],
            end=snapshot["period"]["end"],
        ),
    }
    advice = build_recommendations(snapshot, question)
    try:
        _rate(organization, actor)
        ranking = _external(advice, snapshot, organization, actor)
    except MarketingAIUnavailable:
        advice["notice"] = (
            "OpenRouter tidak tersedia untuk permintaan ini; saran lokal berbasis aturan ditampilkan. Angka dihitung aplikasi, dampak cash-in tidak dijamin."
        )
    else:
        by_id = {item["id"]: item for item in advice["recommendations"]}
        advice["recommendations"] = [by_id[identifier] for identifier in ranking]
        advice.update(
            mode="openrouter",
            mode_label="Prioritas AI · angka dihitung aplikasi",
            notice="Model AI mengurutkan saran yang telah dihitung aplikasi. Hanya agregat dan kode terbatas dikirim; identitas serta pertanyaan tetap lokal. Dampak cash-in tidak dijamin.",
        )
    _membership(organization, actor)
    advice["source_references"] = snapshot["management"]["source_references"]
    advice["snapshot_id"] = advice_identity(snapshot)[:24]
    record(
        organization,
        actor,
        "marketing.advice.created",
        detail={
            "mode": advice["mode"],
            "snapshot_id": advice["snapshot_id"],
            "formula_version": snapshot["formula_version"],
            "recommendation_ids": [item["id"] for item in advice["recommendations"]],
        },
    )
    return advice


def advice_identity(snapshot):
    """Bind local recommendations to source identity as well as their totals."""
    material = {
        "period": snapshot["period"],
        "facts": _safe_facts(snapshot),
        "filters": snapshot.get("filters", {}),
        "formula_version": snapshot["formula_version"],
        "ad_sources": snapshot["ad_sources"]["fingerprint"],
        "management": snapshot["management"]["fingerprint"],
        "invoice_attribution": snapshot.get("invoice_attribution_fingerprint", ""),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
