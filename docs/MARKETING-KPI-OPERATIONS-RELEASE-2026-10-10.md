# Rilis terbatas MKT-04 — KPI penjualan dan follow-up

Tanggal: 10 Oktober 2026
Cakupan: perubahan lokal di modul Marketing; bukan persetujuan kesiapan seluruh ERP.

## Perubahan

- Target dan analisis membedakan prospek yang sudah membayar sebagian/apa pun dari prospek dengan seluruh invoice aktif bernilai neto yang lunas penuh.
- Pembatalan dan kredit mengurangi nilai invoice sebelum status lunas dinilai. Pelunasan uang muka yang sudah diterapkan serta kredit pajak pelanggan yang telah diverifikasi masuk dalam fakta pembayaran. Pembayaran parsial tidak pernah dihitung sebagai pesanan lunas.
- KPI follow-up memakai semua janji dengan tenggat pada periode sebagai penyebut. Janji tanpa completion tetap masuk penyebut; completion harus dicatat dan terjadi paling lambat saat tenggat. Koreksi yang baru dicatat setelah tenggat tidak bisa mengubah hasil menjadi tepat waktu.
- Ringkasan menjelaskan tanggal cutoff pengamatan. Janji yang belum jatuh tempo saat cutoff tidak dinilai untuk periode tersebut.
- Persetujuan sumber KPI mencocokkan jumlah prospek atau semua janji jatuh tempo dengan manifest lingkup. KPI pembayaran memerlukan cakupan mutasi bank Finance yang diperiksa serta fingerprint yang sama.
- Jika OpenRouter gagal atau tidak tersedia, saran lokal tetap ditampilkan dengan pemberitahuan yang menyebut kegagalan layanan eksternal. OpenRouter hanya mengurutkan rekomendasi yang dibentuk aplikasi; ia tidak mengubah angka atau membuat tindakan.

## Formula dan batas interpretasi

**Lunas penuh** berarti jumlah pembayaran yang benar-benar diterapkan ke seluruh invoice aktif bernilai neto pada sebuah pesanan mencapai seluruh nilai neto invoice tersebut. Invoice batal/nilai neto nol tidak menjadikan pesanan sebagai transaksi lunas. Nilai ini mengukur pesanan yang tercatat di ERP dan tidak membuktikan semua penjualan eksternal telah dimasukkan.

**Follow-up tepat waktu** = janji yang jatuh tempo dalam periode, memiliki penyelesaian tercatat, dan waktu aktual penyelesaiannya tidak melewati tenggat ÷ seluruh janji yang jatuh tempo × 100%. Dimensi anggota mengikuti PIC prospek saat pendaftaran, bukan akun karyawan yang mencatat kontak.

Persetujuan sumber follow-up adalah kontrol kelengkapan catatan ERP, bukan bukti bahwa semua komunikasi eksternal telah diimpor. Rasio kas terhadap belanja tetap bukan laba, ROI, ataupun hubungan sebab-akibat. Tidak ada skor gabungan atau penilaian staf otomatis.

## Pemeriksaan

- Suite Marketing lengkap setelah implementasi KPI: **295 lulus, 14 dilewati** di SQLite. Kasus dilewati memerlukan PostgreSQL/fitur konkuren.
- Perubahan fallback OpenRouter: tes regresi terlebih dahulu gagal pada pesan fallback yang tidak spesifik; setelah perbaikan tes terkait lulus.
- Pemeriksaan Django, migrasi (`makemigrations --check --dry-run`), Ruff terpilih untuk berkas Python tersentuh, dan `git diff --check`: lulus.
- Database perusahaan tidak dipakai oleh tes dan tidak diubah oleh rilis kode ini.

## Batas dan langkah penerapan

Uji PostgreSQL khusus untuk jalur MKT-04, UAT oleh staf, peluncuran cloud/HTTPS, serta impor otomatis Meta/Google/WhatsApp belum dilakukan. KPI hasil resmi baru boleh disahkan setelah Finance dan Direktur memastikan periode serta sumber lengkap. Belum ada migrasi data bisnis yang dijalankan untuk rilis ini. Catatan keseluruhan tetap `all_erp_ready: false`.

Sebelum deploy, jalankan migrasi baru pada salinan staging, verifikasi rencana migrasi yang aman, lalu uji login, permission peran, data KPI, dan backup. Ikuti prosedur rilis umum untuk domain/server.
