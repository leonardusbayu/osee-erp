from dataclasses import dataclass

@dataclass(frozen=True)
class Module:
    key: str
    label: str
    description: str
    icon: str
    available: bool
    url: str = ""

MODULES = (
    Module("finance", "Finance", "Penjualan, bank, pembukuan, laporan, dan pajak dalam satu alur.", "wallet", True, "/"),
    Module("director", "Direktur", "Pertumbuhan, kas, marketing, anggaran, dan arah perusahaan.", "chart", True, "/director/"),
    Module("marketing", "Marketing", "Analisis iklan, prospek, performa tim, dan saran untuk meningkatkan cash-in.", "users", True, "/marketing/"),
    Module("test_operations", "Operasional", "Modul berikutnya untuk jadwal, kuota, peserta, dan penyelesaian tes yang terhubung ke Finance. Akses tim Operasional akan ditambahkan bersama modul ini.", "calendar", False),
    Module("learning", "Akademik & Kursus", "Kelas, pengajar, kehadiran, dan progres belajar.", "book", False),
    Module("partners", "Portal Mitra", "Pemesanan mandiri dan harga sesuai perjanjian setiap mitra.", "users", False),
    Module("people", "SDM & Penggajian", "Dikelola oleh Finance: data pegawai, perhitungan gaji, dan persiapan pajak pegawai. Rencana persetujuan penggajian oleh Owner/Direktur.", "briefcase", False),
)
