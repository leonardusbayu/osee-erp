# Status ERP OSEE — 10 Oktober 2026

`all_erp_ready: false`. Tujuan seluruh aplikasi tetap terbuka. Pembaruan ini menerima alur FIN04 yang dijelaskan di bawah; [status sebelumnya](FULL-APP-STATUS-2026-10-09.md) tetap menjadi bukti historis untuk batch lainnya.

Source terpasang: `bf4a8d16ce3ecf07fb406786308a3735f9ca2feb7bc8e0d9b7902d26a438f9c7`. Status cakupan: **READY_FOR_SCOPE / installed_local**. Aplikasi lokal: [Piutang & utang lama](http://127.0.0.1:8001/finance/opening-balances/). Ini belum merupakan deployment cloud/domain/HTTPS.

## Alur yang diterima

Owner/Direktur mencatat sisa piutang pelanggan/mitra dan utang pemasok dari dokumen asli pada satu tanggal awal. Finance/Owner mencocokkan pembayaran sebagian atau penuh dengan mutasi bank nyata. Formulir memakai tiga langkah: isi, periksa sumber, konfirmasi. Posisi historis dan laporan menghitung saldo yang masih tersisa; bukti rusak tetap terlihat sebagai belum dapat diperiksa.

Saldo awal dan pelunasannya tidak membuat penjualan/biaya baru. Pemeriksaan pajak atas kas asli terpisah, tetap terlihat setelah lunas, dan memerlukan bukti profesional. Penerimaan piutang lama tidak otomatis ditetapkan sebagai omzet atau kredit pajak.

| Bukti aktual pada source terpasang | Hasil |
|---|---|
| Regresi SQLite seluruh aplikasi | 1101 lulus / 61 skip dari 1162 kasus |
| Kasus terkait PostgreSQL, termasuk transaksi bersamaan | 94 lulus, tanpa skip |
| Oracle publik asli sebelum/sesudah perbaikan | 2 kasus fitur dan 1 kasus organisasi dengan 3 assertion; RED asli dipertahankan, replay lulus |
| Chrome desktop/ponsel | 8 alur / 156 keadaan; 0 error skrip/overflow; Finance ponsel tanpa JavaScript |
| Pemeriksaan sistem, migrasi, lint terpilih, kompilasi, dependensi | Lulus |
| Runtime perusahaan | 11 rute/status/aset diperiksa; akses anonim dilindungi |
| Data sebelum/sesudah pemasangan | 1010 baris asli / 130 tabel asli dipertahankan; unggahan, nilai, kolom, urutan bisnis dan waktu migrasi lama tetap |

Backup database/media serta source dibuat dan diverifikasi sebelum pemasangan. Dua migrasi menambahkan tiga register bisnis kosong; hanya 15 definisi framework ditambahkan. Tidak ada transaksi, saldo, pengguna, target atau data pajak perusahaan yang dikarang/dimasukkan untuk pengujian.

## Pekerjaan berikutnya

1. Koreksi saldo awal dan pembalikan/refund pembayaran dengan riwayat tetap dan kontrol periode.
2. Kas saldo awal dengan pemotongan positif serta kredit/kewajiban pajak lama; akun saldo awal lainnya.
3. Kelengkapan sumber perusahaan, pemeriksaan pajak yang diterima, dan UAT staf nyata.
4. Backlog lain pada status sebelumnya: cabang bulanan/tahunan/pembetulan pajak, publikasi sumber hukum, perencanaan Direktur, integrasi KPI/marketing selain cakupan MKT-04 di bawah, Operations lanjutan, validasi pajak dan data SDM/penggajian, portal mitra, serta cloud/HTTPS/backup eksternal.

BNI API masih menunggu persetujuan; impor statement manual tetap menjadi alur yang tersedia. Konfigurasi akun kanal pemasaran, OpenRouter, server/domain dan sumber fiskal asli memerlukan akses/data yang nyata. Penerimaan teknis ini tidak menyatakan seluruh ERP siap digunakan perusahaan tanpa batas.

Rincian kontrak: [rencana FIN04](OPENING-AR-AP-PLAN-2026-10-10.md). Bukti, riwayat percobaan dan batas: [rilis FIN04](OPENING-AR-AP-RELEASE-2026-10-10.md).

## Tambahan verifikasi Operasi — 10 Oktober 2026

OPS-01 (koreksi kehadiran) dan OPS-02 (koreksi identitas peserta) kini memiliki alur terkendali dengan riwayat. Uji 25 kasus Operasi lulus 22 kasus dan melewati 3 kasus khusus PostgreSQL karena runner lokal memakai SQLite. QA terpilih memberi `READY_FOR_SCOPE` untuk tes layanan dan HTTP; konkurensi koreksi PostgreSQL tetap belum diverifikasi. Ini bukan penerimaan seluruh ERP atau deployment produksi. Rincian dan bukti: [rilis koreksi Operasi](OPERATIONS-CORRECTIONS-RELEASE-2026-10-10.md) dan [paket QA](qa-runs/operations-audited-corrections-2026-10-10-01/report.md).

## Tambahan verifikasi Marketing — 10 Oktober 2026

MKT-04 menambahkan KPI hasil pesanan lunas penuh (terpisah dari pembayaran sebagian) dan ketepatan janji follow-up, dengan sumber, denominator, riwayat completion, dan batas pengamatan yang eksplisit. Evaluasi KPI resmi memerlukan pencocokan jumlah prospek dan janji yang disahkan; KPI pembayaran juga memerlukan cakupan mutasi bank yang sudah diverifikasi Finance. Saran AI menunjukkan alasan lokal saat OpenRouter tidak aktif atau gagal. Uji Marketing pada SQLite: 295 lulus, 14 dilewati karena perlu PostgreSQL; perubahan fallback AI diuji ulang terpisah setelah paket tersebut. Belum ada UAT staf nyata, tes konkurensi PostgreSQL baru, atau deployment cloud dalam cakupan ini. Detail dan formula: [rilis MKT-04](MARKETING-KPI-OPERATIONS-RELEASE-2026-10-10.md) dan [panduan KPI & audit](MARKETING-KPI-AND-AUDIT.md).

## Tambahan verifikasi SDM & penggajian — 10 Oktober 2026

Alur Finance untuk daftar karyawan/tutor, nominal yang sudah diperiksa, persetujuan terpisah Owner/Direktur, jurnal, pencocokan bank, revisi dan riwayat kini aktif di aplikasi lokal. Backup database diverifikasi sebelum migrasi; seluruh row count bisnis 138 tabel lama tetap sama, empat tabel payroll baru kosong, dan metadata framework bertambah sesuai model baru. Uji SQLite in-memory lulus 14/14; smoke test lokal memberi login 200 dan redirect payroll anonim ke login. Gate `NEEDS_EVIDENCE` karena UAT visual/responsif, PostgreSQL, login staf, validasi data aktual, dan cloud belum diuji. Nominal PPh 21/BPJS tetap manual dan memerlukan pemeriksaan eksternal. Lihat [rilis SDM & penggajian](PEOPLE-PAYROLL-RELEASE-2026-10-10.md) dan [hasil QA terinci](qa-runs/people-payroll-2026-10-10-05/report-final.md).
