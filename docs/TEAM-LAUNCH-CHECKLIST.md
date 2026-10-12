# Checklist mulai kerja tim dan cloud OSEE

Tanggal penyiapan: 2 Oktober 2026. Checklist ini berisi pekerjaan yang harus diisi dan disahkan oleh orang yang bertanggung jawab; tidak membuat akun staf, target, transaksi, secrets, atau layanan cloud.

**Status saat disiapkan:** aplikasi dan konfigurasi deployment sudah tersedia. Server/domain publik, HTTPS nyata, backup di luar host, penerimaan staf, dan cutover belum dikonfirmasi. API bank, platform marketing, WhatsApp, dan AI eksternal tidak diaktifkan oleh checklist ini.

## 1. Orang, akses, dan PIC

- [ ] Owner menetapkan nama orang dan akun pribadi untuk setiap peran. Isi daftar berikut secara privat; jangan mencatat sandi atau kode MFA di dokumen ini.

| Tanggung jawab | Nama/akun yang ditetapkan | Bukti pemeriksaan |
| --- | --- | --- |
| Owner/Direktur: akses, target, sumber, pemeriksaan hasil | Belum diisi | Belum diperiksa |
| Finance: pesanan, invoice, mutasi, kontrol total | Belum diisi | Belum diperiksa |
| Marketing: prospek, PIC, tindak lanjut, bukti kerja | Belum diisi | Belum diperiksa |
| Administrator server/DNS/backup dan pengganti | Belum diisi | Belum diperiksa |

- [ ] Owner membuat atau memeriksa akun melalui **Pengaturan**. Setiap staf mengganti sandi sementara, menyiapkan MFA dan menyimpan pemulihan secara privat; cloud mewajibkan MFA.
- [ ] Di **Marketing → Tim**, hubungkan PIC dengan akun pribadi yang benar. Tetapkan alih tugas/delegasi dan pemeriksa bukti yang berbeda dari pengaju.
- [ ] Uji masuk sebagai masing-masing peran. Marketing hanya memperoleh data yang berwenang; Finance/Owner memeriksa akses dokumen privat. Nonaktifkan akses orang yang keluar melalui alur aplikasi.

## 2. Kebijakan kerja, target, dan sumber nyata

- [ ] Owner menyetujui hari/jam kerja, hari libur, dan tenggat respons/follow-up yang benar. Nilai contoh atau bawaan belum merupakan keputusan perusahaan.
- [ ] Tim mengusulkan KPI dengan angka, satuan, periode, lingkup PIC/kanal/kampanye dan alasan yang nyata; Owner memeriksa lalu **Setujui & aktifkan**. Jangan mengisi angka agar checklist tampak selesai.
- [ ] Finance/Marketing mendaftarkan seluruh sumber periode: prospek/pendaftaran, invoice/pembayaran, belanja kampanye, dan bukti pekerjaan yang relevan. Catat cakupan periode, jumlah/nominal kontrol, penanggung jawab, serta sumber yang belum tersedia.
- [ ] Owner memeriksa lalu mengesahkan kelengkapan sumber. Nol hanya dipakai bila didukung pemeriksaan sumber; data belum diketahui tetap dinyatakan belum lengkap.
- [ ] Setelah periode berakhir, simpan evaluasi resmi dengan sumber yang disahkan. Perubahan sumber, atribusi, atau bukti memerlukan peninjauan ulang evaluasi lama.

Panduan rinci: [alur operasional](WORKFLOW-IMPLEMENTATION-2026-10-02.md), [KPI dan bukti pekerjaan](MARKETING-KPI-AND-AUDIT.md), serta [Finance](FINANCE-OPERATING-GUIDE.md).

## 3. Input cloud dan pemulihan

- [ ] Konfirmasikan hostname ERP persis, email operasional, penyedia/host Linux, IP publik, akses administrator bernama dan pengelola DNS. `erp.example.invalid` dalam contoh tidak dapat memperoleh sertifikat publik.
- [ ] Administrator meninjau Docker/Compose, firewall dan DNS A/AAAA. Hanya proxy membuka 80/443; aplikasi 8000 dan PostgreSQL 5432 tetap privat. Jika ada CDN/proxy tambahan, tinjau ulang batas kepercayaan proxy.
- [ ] Catat commit release, hasil CI untuk commit itu dan versi/digest image yang diterima. `origin` sudah dikonfigurasi ke [osee-erp](https://github.com/leonardusbayu/osee-erp.git); status push, perlindungan repository dan hasil CI harus diperiksa untuk release yang dipilih.
- [ ] Siapkan secrets melalui administrator dan vault terlindungi. Untuk perusahaan yang dipindahkan, pertahankan Django signing key dan OTP encryption key asli; jangan menggantinya dengan hasil generator baru. Jangan kirim nilainya melalui chat/Git.
- [ ] Tetapkan lokasi backup terenkripsi di luar host, retensi, jadwal, pemeriksaan kegagalan, pemilik tugas, RPO dan RTO. Backup satu disk belum cukup untuk pemulihan saat host hilang.
- [ ] Setujui jendela pemeliharaan, penanggung jawab cutover, kontrol total Finance dan rencana rollback code + database + dokumen + secrets yang kompatibel.

## 4. Urutan pemindahan perusahaan SQLite yang sudah ada

Urutan ini berbeda dari inisialisasi perusahaan baru. Gunakan prosedur lengkap di [deployment dan recovery](../deploy/README.md#sqlite-to-postgresql-handover).

1. Bekukan release yang diperiksa. Dalam jendela pemeliharaan, hentikan semua penulis pada aplikasi sumber, termasuk launcher, impor dan perintah operator. Buat/verifikasi backup privat serta salinan rollback.
2. Bangun lingkungan tujuan dengan hostname/secrets yang disetujui. Jalankan database saja; aplikasi tujuan tetap berhenti. Administrator menyiapkan **database PostgreSQL kosong dan direktori media privat kosong** yang akan dipakai deployment.
3. **Jangan jalankan `manage.py migrate` pada tujuan sebelum transfer.** `scripts/sqlite_to_postgres.py` menolak tabel yang sudah ada dan membuat skema dalam proses transfer. Pakai workspace baru, tujuan eksplisit, akses terbatas dan `--quiesced` hanya setelah semua penulis benar-benar berhenti.
4. Jalankan transfer dari lingkungan administrator tepercaya, sebaiknya pada host tujuan. Periksa hasil `verification.json`, kesamaan hash/jumlah setiap tabel, hash dokumen, dan bahwa sumber tidak berubah. Kesalahan meninggalkan tujuan yang belum diterima; periksa masalah dan gunakan tujuan kosong baru untuk pengulangan.
5. Setelah verifikasi berhasil, periksa migrasi/config produksi dan readiness internal. Mulai aplikasi hanya untuk UAT terbatas; sumber tetap tidak menerima penulisan selama keputusan cutover agar kedua sistem tidak menyimpang.
6. Finance membandingkan kontrol total dan selisih sumber/buku. Staf menguji peran, MFA, PIC, sumber/KPI, bukti dan unduhan privat sebelum akses kerja normal dibuka.
7. Administrator menguji HTTPS dan pemulihan di luar host; Owner/Finance mencatat penerimaan atau penghalang. Buka trafik kerja normal/DNS cutover hanya sesuai keputusan tersebut. Jika perlu rollback, gunakan set release/database/media/secrets yang cocok dan catat keputusan.

## 5. Bukti penerimaan sebelum dipakai bersama

- [ ] Sertifikat valid untuk hostname yang benar, HTTP beralih ke HTTPS, dan aplikasi dapat diakses staf dari perangkat yang akan dipakai. `/health/` dan `/ready/` sengaja tidak publik (404); readiness diuji dari jaringan internal.
- [ ] Owner, Finance dan Marketing mencoba alur perannya pada data pengujian yang terpisah: masuk/MFA, tugas dan delegasi, pengajuan/pemeriksaan bukti, kelengkapan sumber dan KPI, serta pesanan/invoice/pembayaran sesuai panduan. Pemeriksaan memakai data sintetis agar tidak menciptakan transaksi perusahaan sebagai tes.
- [ ] Akses yang tidak berwenang ditolak dan riwayat pelaku/waktu/alasan dapat ditelusuri. Dokumen privat tidak dapat dibuka tanpa izin.
- [ ] Backup di luar host sudah diverifikasi dan dipulihkan pada tujuan terpisah; hasil dan waktu pemulihan dibandingkan dengan RPO/RTO yang disepakati.
- [ ] Catat tanggal, commit, nama pemeriksa, hasil, penghalang dan keputusan mulai kerja. Kemampuan yang belum diterima tetap ditampilkan sebagai belum aktif.

Bukti pengembangan yang sudah tercatat: [laporan 2 Oktober 2026](WORKFLOW-IMPLEMENTATION-2026-10-02.md) mencatat **535 tes PostgreSQL lulus** untuk sumber yang direkam di sana. Perubahan panduan mulai berikutnya masih diuji; hasil sebelumnya bukan bukti deployment cloud atau penerimaan staf.
