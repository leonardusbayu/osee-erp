# Implementasi alur OSEE — 2 Oktober 2026

Dokumen pendamping `MARKETING-WORKFLOW-GAPS-2026-10-02.md`. Laporan audit lama adalah bukti kondisi sebelum implementasi; tidak ditimpa. Perubahan ini menambah alur operasional dan kontrol, bukan mengisi transaksi atau target perusahaan secara otomatis.

Pembaruan 11 Oktober 2026: gap kursus rinci per pertemuan yang dicatat pada tabel di bawah sudah memiliki alur lokal untuk paket, jadwal, kehadiran, koreksi, dan audit. Kebijakan pengakuan pendapatan kursus serta UAT tetap terbuka; lihat [rilis Operations kursus](COURSE-MEETINGS-RELEASE-2026-10-11.md).

## Alur penggunaan

1. Owner membuat akun pribadi Finance/Marketing pada Pengaturan. Di Marketing → Tim, hubungkan setiap PIC dengan akun. Atur kalender kerja dan tenggat pada kebijakan kerja; ambang tidak diasumsikan sebagai kebijakan perusahaan sebelum disetujui.
2. Marketing mencatat prospek, menetapkan PIC, dan mencatat kontak/hasil/janji pada linimasa. Alih tugas dan delegasi memiliki alasan serta histori; PIC operasional dipisahkan dari atribusi awal KPI.
3. Finance membuka **Pesanan & Peserta**. Pilih pelanggan/mitra yang ditagih, tambahkan produk dan jumlah unit, lalu hubungkan prospek asal. Pendaftaran peserta berbeda dari identitas pembeli.
4. Untuk CSV/XLSX pendaftaran: unggah → petakan kolom → periksa duplikat → pilih pesanan/pemesan dan produk → terima baris. ID respons tetap lebih baik daripada nomor baris. Tanpa ID tetap, sistem memperingatkan identitas snapshot. Baris sumber tidak ditimpa; tidak mengunduh tautan KTP/bukti transfer.
5. Gunakan invoice yang sudah ada bila sesuai, atau periksa pratinjau harga lalu terbitkan invoice. ITP, iBT, dan kursus menjadi rincian terpisah. Satu peluang pembelian dapat memiliki beberapa invoice melalui satu pesanan; setiap invoice diatribusikan sekali.
6. Finance mengimpor dan mencocokkan mutasi. Untuk mitra, seluruh komponen pesanan yang masih ditagih harus lunas sebelum pelaksanaan. Pendaftaran atau tahap “sepakat” bukan bukti pembayaran.
7. Buat sesi dan kapasitas → jadwalkan peserta → catat kehadiran → konfirmasikan layanan selesai. Penyelesaian satu peserta mengakui satu unit pendapatan. Pindah sesi menyimpan alasan dan tidak mengubah pendapatan.
8. Owner menyetujui pembatalan/koreksi per unit. Koreksi mengurangi piutang atau membentuk kewajiban refund sesuai pembayaran yang sudah diterima. Finance mencocokkan refund dengan mutasi keluar sebenarnya; aplikasi tidak mengirim uang.
9. Finance mengalokasikan biaya provider dari tagihan yang sudah disetujui ke unit selesai. Alokasi bukan pembebanan ulang jurnal. Biaya yang masih dibayar di muka harus dilepas sesuai bukti layanan sebelum dipakai sebagai biaya aktual. Margin belum ditampilkan sebagai diketahui jika cakupan biaya belum lengkap.
10. Marketing menjalankan pekerjaan dengan bukti → pengajuan → pemeriksaan independen → penerimaan/revisi. Rekomendasi lama dihubungkan ke pekerjaan yang sama. Pekerjaan dapat terkait target KPI, keputusan, dan anggaran; persetujuan pekerjaan tidak otomatis menyetujui pengeluaran.
11. Publikasi konten/SEO dicatat setelah pekerjaan diterima, dengan URL, tanggal, topik, dan bukti. Pengukuran sumber dan koreksinya memiliki histori. Tidak memublikasikan ke kanal luar atau mengarang skor/hasil SEO.
12. Sebelum evaluasi KPI resmi, Finance/Marketing mendaftarkan seluruh sumber periode, kontrol jumlah dan bukti. Owner memeriksa kelengkapan. Koreksi sumber, atribusi, atau bukti membuat evaluasi lama ditandai perlu diperiksa ulang.

## Status terhadap temuan audit

| Gap | Yang sudah dibangun | Batas yang masih berlaku |
|---|---|---|
| 01: tagihan pemasok ganda | Referensi pemasok dinormalisasi bersama pada entri/impor; pengulangan cocok digunakan kembali, konflik ditahan; perlindungan konkurensi | Duplikat lama tidak digabung/dihapus otomatis |
| 02: belum ada data pengukuran | Daftar sumber periode, pemeriksaan, akun/PIC, pengaitan dan antrean pengecualian | Pengisian akun staf, sumber aktual, target, dan rekonsiliasi awal masih diperlukan oleh perusahaan |
| 03–04: PIC dan tanggung jawab | Penugasan, alih tugas, akun–PIC, delegasi tanggal tertentu, supervisor dengan alasan eksplisit | Target historis tetap memakai atribusi awal; bukan perpindahan hasil diam-diam |
| 05: pendaftaran sampai tes | Impor bertahap, pesanan, peserta, invoice, kapasitas, sesi, kehadiran dan penyelesaian | Konfirmasi pembeli/mitra dan prospek masih eksplisit; belum sinkronisasi langsung Google Sheets/form/WhatsApp |
| 06: kelengkapan KPI | Inventaris sumber dan pengesahan per periode, nol terverifikasi, deteksi dasar berubah | Pengesahan manusia bukan jaminan dari API bahwa seluruh aktivitas eksternal sudah tertangkap |
| 07: dua alur pekerjaan | Rekomendasi menjadi WorkActivity, penutupan melalui pekerjaan diterima | Riwayat lama dipertahankan |
| 08: rekonsiliasi bank | Kontrol jumlah/baris/saldo, pratinjau, pengesahan, pengganti pemeriksaan, cakupan periode dan rekonsiliasi saldo ke buku besar sebelum tutup buku | API BNI tetap menunggu persetujuan; sumber Excel/CSV manual |
| 09: pelaksanaan sebagian | Pendapatan per unit, pembatalan sebagian, kredit layanan, refund tertaut dan kontrol periode | Alur pembelajaran kursus rinci per pertemuan belum dibangun |
| 10: unit bisnis/margin/harga | Pesanan vs peserta vs invoice; pesanan lunas; refund tertaut; biaya provider aktual; harga khusus disetujui Owner dan terikat satu invoice | Margin setelah biaya provider belum laba bersih; model atribusi iklan kausal dan KPI komersial baru khusus per produk belum tersedia |
| 11: antrean harian | PIC/kontak/janji, kalender kerja, SLA yang disahkan, antrean tanpa duplikasi | Pengingat dalam aplikasi; tidak mengirim WhatsApp/email |
| 12: siklus kampanye | Jeda, lanjut, selesai, serah tanggung jawab dan tanggal akhir aktual | Tidak mengubah status kampanye langsung di Meta/Google |
| 13: Direktur/AI | Ringkasan KPI, sumber, pekerjaan, biaya dan fakta pesanan bersama; hubungan keputusan–anggaran–pekerjaan | AI memberi saran, tidak menjalankan pembayaran/iklan/kebijakan. Perlu persetujuan pengungkapan baru untuk konteks AI yang diperluas |
| 14: koreksi bukti/sumber | Keputusan pengecualian dengan PIC, evidence privat, revisi/koreksi tercatat, bukti lama tetap ada | Tidak mengoreksi sumber historis secara diam-diam |
| 15: kanal khusus | Konten/SEO dan pengukuran, kampanye, kontak, mitra/pesanan tersedia sebagai pencatatan terkontrol | Konektor kanal, portal mitra mandiri, otomasi SEO/publikasi dan operasi kanal lengkap tetap pengembangan berikutnya |

## Makna angka pada laporan

- Arus kas marketing yang lama adalah penerimaan bruto yang telah dialokasikan. Ringkasan pesanan menyediakan refund tertaut secara terpisah. Kas uang muka yang belum teratribusi tidak diasumsikan sebagai penjualan kanal tertentu.
- Pesanan lunas tidak sama dengan layanan selesai. Komponen yang seluruhnya dikoreksi tidak menghalangi komponen pesanan tersisa yang sudah lunas; pesanan tanpa nilai tersisa tidak dihitung sebagai keberhasilan penjualan.
- Margin unit selesai = pendapatan layanan neto dalam kelompok pesanan − biaya provider aktual tertaut. Biaya marketing, payroll, overhead, dan pajak bukan bagian dari angka ini.
- Kelompok pesanan mengikuti tanggal pesanan. “Tanpa pesanan terdahulu yang kini lunas” bukan tanggal pertama pelanggan membayar. Jumlah roster/jumlah unit sebelum penagihan mengikuti keadaan yang tercatat saat laporan dibuka; tanggal bukti penyelesaian menjadi dasar layanan historis.
- Pengesahan sumber dan evaluasi KPI menyimpan dasar/fingerprint. Angka baru tidak menimpa evaluasi lama; perubahan dasar memerlukan peninjauan kembali.
- Median respons KPI yang sudah ada tetap memakai menit kalender sejak prospek diterima. Batas waktu pada antrean harian memakai kalender kerja yang disahkan. Keduanya adalah ukuran berbeda dan tidak boleh dibandingkan sebagai rumus yang sama.

## Privasi dan batas akses

Pesanan, kontak peserta, sumber pendaftaran dan koreksi keuangan dibatasi ke Finance/Owner. Marketing bekerja melalui data prospek dan tugas yang berwenang. Tidak ada akun/pengaturan provider baru yang dibuat otomatis. Bukti privat menggunakan unduhan terotorisasi dan pemeriksaan integritas. AI menerima agregat dalam daftar yang diizinkan, bukan baris pendaftaran, KTP, kontak, dokumen, atau mutasi mentah. Pengungkapan versi baru harus disetujui sebelum konteks yang diperluas dikirim.

## Validasi dan serah terima

Pengujian menggunakan database sintetis terpisah. Bukti browser Marketing: `.local/marketing-performance-qa/run-c8cghl0w` (22 pemeriksaan halaman desktop/mobile dan 5 alur). Bukti browser operasi: `.local/operations-qa/run-6ehbbuv4` (14 pemeriksaan halaman, 3 alur, termasuk impor/konfirmasi, harga invoice, peserta selesai dan akses ditolak). Pengujian keuangan memakai nilai acuan eksplisit, termasuk kelompok 2 × Rp510.000 dan biaya 2 × Rp450.000.

Hasil final:

- PostgreSQL 17: **535 tes lulus, tanpa skip**, termasuk kapasitas sesi, batas biaya, pemakaian tunggal persetujuan harga, invoice pemasok, penutupan periode, sumber/bukti, KPI dan persetujuan pekerjaan pada permintaan bersamaan.
- SQLite: **522 tes lulus; 13 tes khusus PostgreSQL dilewati** dan telah lulus pada PostgreSQL. Total kasus yang ditemukan: 535.
- Kedua pengujian penuh memakai sumber yang sama dan tidak berubah selama pengujian: `be71312d62f630be37e43afb4068fb1a9c4b4b68a8535dbdcb2124f3c7eb3a0f`. Bukti: `.local/fix-2026-10-02/verification/workflow-bottomup-{sqlite,postgres}-final.*`.
- Pemeriksaan konfigurasi Django, konsistensi migrasi, dan whitespace perubahan lulus.
- Dua belas migrasi diuji pada salinan database, lalu diterapkan ke aplikasi lokal saat server berhenti. **Baris dan kolom lama pada 83 tabel dipertahankan**; registrasi model/izin Django mendapat tambahan yang diperlukan. Dua puluh tujuh tabel baru kosong, tanpa transaksi contoh.
- Integritas database dan foreign key lulus. Backup sebelum/sesudah migrasi terverifikasi tersedia di `.local/workflow-release-2026-10-02/`; detail di `apply-result.json`.
- Server lokal kembali aktif. `/health/` dan `/ready/` mengembalikan HTTP 200, masing-masing `ok` dan `ready`.

Implementasi ini tidak mengubah aturan pajak atau menjamin otomatisasi pengajuan DJP. Cloud/domain/HTTPS dan API eksternal harus diverifikasi di lingkungan yang akan dipakai staf; kelulusan pengujian lokal tidak menggantikan pemeriksaan tersebut.

## Panduan penyiapan tim setelah implementasi

Halaman `/settings/get-started/` menyediakan urutan penyiapan akun, akun–PIC, kalender, harga, pendaftaran, sumber bank, pengecualian impor, dan target seimbang. Owner/Finance dapat membacanya; tindakan khusus Owner hanya ditautkan kepada Owner. Panduan tersedia dari navigasi Finance dan Direktur.

Status dihitung dari catatan perusahaan terpilih, bukan persentase kesiapan produksi. Membuka halaman tidak membuat akun, target, kebijakan, atau transaksi. Akun dicabut/nonaktif tidak dianggap sebagai PIC yang telah terhubung. Tanpa impor sumber keuangan, langkah pengecualian tetap perlu dilengkapi. Kehadiran harga/pendaftaran/target belum membuktikan seluruh sumber atau kontrak telah diperiksa.

Validasi penambahan ini menggunakan data sintetis terpisah: 33 tes HTTP panduan, peran tim, dan Finance lulus; empat tes panduan lulus kembali setelah penandaan sumber kosong diperbaiki. Pemeriksaan konfigurasi dan konsistensi migrasi lulus, tanpa migrasi baru. Browser memeriksa 24 halaman desktop/ponsel dan lima alur KPI/bukti/akses; tangkapan panduan 1440/390 piksel diperiksa. Bukti lokal berada di `.local/fix-2026-10-02/verification/onboarding-*`, `.local/marketing-performance-qa/run-07pq7kg9/`, dan `.local/onboarding-qa/`. Hasil 535 tes PostgreSQL di atas adalah bukti implementasi sebelumnya; suite penuh tersebut belum diulang untuk halaman panduan ini.

[Checklist peluncuran tim dan cloud](TEAM-LAUNCH-CHECKLIST.md) serta [panduan deployment](../deploy/README.md) mencatat urutan pemindahan perusahaan yang sudah ada ke target PostgreSQL kosong. Pembuatan staf nyata, kalender/angka target perusahaan, persetujuan sumber, konfigurasi provider, dan deployment publik masih memerlukan input nyata serta pemeriksaan orang yang bertanggung jawab. Domain/server tujuan dan aksesnya belum diberikan; aplikasi tetap berjalan lokal.
