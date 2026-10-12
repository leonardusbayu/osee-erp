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
    Module(
        "finance",
        "Finance",
        "Penjualan, bank, pembukuan, laporan, dan pajak dalam satu alur.",
        "wallet",
        True,
        "/",
    ),
    Module(
        "director",
        "Direktur",
        "Pertumbuhan, kas, marketing, anggaran, dan arah perusahaan.",
        "chart",
        True,
        "/director/",
    ),
    Module(
        "marketing",
        "Marketing",
        "Hubungi calon pelanggan, kerjakan tugas, dan lihat pencapaian target.",
        "users",
        True,
        "/marketing/today/",
    ),
    Module(
        "test_operations",
        "Pesanan & Peserta",
        "Operations mengelola pendaftaran, peserta, jadwal, dan kehadiran. Finance menangani invoice dan pembayaran.",
        "calendar",
        True,
        "/operations/",
    ),
    Module(
        "learning",
        "Akademik & Kursus",
        "Kelas, pengajar, kehadiran, dan progres belajar.",
        "book",
        False,
    ),
    Module(
        "partners",
        "Portal Mitra",
        "Pemesanan mandiri dan harga sesuai perjanjian setiap mitra.",
        "users",
        False,
    ),
    Module(
        "people",
        "SDM & Penggajian",
        "Finance menyiapkan gaji karyawan dan tutor. Owner/Direktur memeriksa, menyetujui, lalu Finance mencocokkan pembayaran bank.",
        "users",
        True,
        "/people/",
    ),
)
