# Rilis lokal Operasi: koreksi kehadiran dan identitas

Tanggal: 10 Oktober 2026
Cakupan: OPS-01 dan OPS-02, implementasi lokal

## Hasil

Halaman peserta sekarang menyediakan koreksi kehadiran sebelum layanan dibukukan dan koreksi nama/email/telepon peserta. Kedua tindakan meminta alasan, versi catatan terbaru, dan konfirmasi staf; perubahan muncul dalam riwayat dengan pengguna dan waktu.

Koreksi identitas membuat catatan peserta baru khusus untuk pendaftaran itu. Catatan sebelum koreksi, sumber dan ID respons asli tetap disimpan. Impor ulang baris sumber yang sama tidak menolak ataupun menimpa koreksi. Pemesan, invoice, prospek/atribusi, layanan, dan pembukuan tidak berubah.

Nama dan kontak sebelum/sesudah hanya disimpan dalam riwayat pendaftaran. Log audit umum hanya mencatat jenis bidang yang berubah dan referensi pendaftaran; log itu tidak menyalin nama, email, nomor telepon, atau alasan bebas.

## Pemeriksaan

- `manage.py test operations --verbosity 2`: 25 tes; 22 lulus dan 3 tes khusus PostgreSQL dilewati pada runner SQLite.
- `ruff check operations --select E4,E7,E9,F`: lulus.
- `ruff format --check operations`: lulus.
- `manage.py check`: lulus.
- Gate QA terpilih: `READY_FOR_SCOPE` untuk tes layanan dan HTTP OPS-01/02.

Bukti lengkap dan pemetaan kasus tersedia di [laporan QA](qa-runs/operations-audited-corrections-2026-10-10-01/report.md). Koneksi/kredensial PostgreSQL belum tersedia di runner ini, jadi konkurensi koreksi belum teruji.

## Batas rilis

Tidak ada migrasi database. Perubahan ini belum dipasang ke server cloud dan bukan pernyataan bahwa ERP penuh siap digunakan perusahaan. OPS-03 (kursus multi-pertemuan/pengakuan bertahap), pengujian concurrency PostgreSQL, UAT staf, serta gap modul lain tetap terbuka.
