# Hasil perbaikan setelah audit 9 September 2026

Laporan ini menindaklanjuti [audit awal](AUDIT-2026-09-09.md). Audit awal dipertahankan sebagai riwayat; temuan dan angka tes di sana menggambarkan versi sebelum perbaikan ini.

## Kontrol yang ditambahkan dan diperbaiki

| Area | Hasil perubahan | Batas yang tetap perlu diperiksa |
| --- | --- | --- |
| Periode tertutup | Posting pada atau sebelum bulan yang sudah ditutup ditolak; penutupan dan posting diserialkan pada organisasi. Penutupan administratif bulan sebelumnya dapat diselesaikan tanpa membuka kembali periode. | Tidak ada pembukaan ulang otomatis atau pembenaran untuk mengganti tanggal transaksi. |
| Pembayaran sebelum tes | Mutasi aktual masa depan ditolak; penyelesaian memeriksa pembayaran/alokasi sampai tanggal layanan. | Bukti penyelesaian layanan tetap harus berasal dari kegiatan nyata. |
| Integritas finance | Status dibaca ulang, transisi terbatas, flag internal sekali pakai, hak akses aktif diperiksa dari database. | Pemilik akses server/database tetap berada pada batas kepercayaan operator. |
| Rekonsiliasi | Biaya bank, modal pemilik, transfer rekening, uang muka/alokasi/refund, pembatalan sebelum layanan, pelepasan biaya dibayar di muka, pelunasan pemasok neto, dan setoran utang potongan tersedia melalui halaman aplikasi. | Pembatalan penuh sebelum layanan; pelunasan neto penuh pada masa potongan yang sama; saldo awal Bank/modal saja. |
| E-statement | Excel/CSV → pemetaan → pratinjau bertanda tangan → konfirmasi atomik. Sumber privat, hash, rentang, validasi tanggal/nominal dan duplikasi disimpan. | Format bank asli belum diuji karena file belum tersedia. API BNI belum disetujui. |
| Pajak | Review profesional terdokumentasi dalam tiga peran tim; perhitungan deterministik dan pemeriksaan dasar/rate/nominal; bukti PDF privat, pemeriksa dan kecocokan kewajiban; kertas kerja tahunan berversi serta pemeriksaan perubahan bukti. | Tidak menetapkan kelayakan pajak PT dari omzet saja. Paket tahunan masih kertas kerja, bukan acceptance format resmi DJP. |
| Rujukan pajak | Snapshot riwayat tidak ditimpa; revisi hanya aktif melalui publikasi pemeriksaan eksplisit; gerbang kesegaran diterapkan pada panduan. | Rujukan dan profil perusahaan tidak otomatis disetujui oleh migrasi. |
| Akun | Revokasi anggota, perlindungan Owner terakhir, reset sandi sementara, pembatalan sesi, MFA TOTP terenkripsi, kode pemulihan sekali pakai dan pemulihan operator yang diaudit. | Aktivasi Authenticator anggota nyata serta tata kelola administrator masih perlu dijalankan. |
| Login dan lampiran | Pembatasan per pasangan akun/IP, per akun dan per IP; parsing PDF dan validasi gambar; unduhan privat. | Bukan sertifikasi uji penetrasi atau layanan antimalware eksternal. |
| Deployment | Konfigurasi cloud PostgreSQL, Caddy HTTPS, Waitress dengan proxy tepercaya, WhiteNoise, secrets terpisah, readiness DB/migrasi/storage, CI dan prosedur operasi. | Container dan domain cloud nyata belum dijalankan. |
| Pemulihan | Backup database+berkas berhash, penolakan restore ke target berisi, latihan restore SQLite/PostgreSQL dan transfer SQLite ke PostgreSQL kosong dengan pemeriksaan seluruh tabel/berkas. | Latihan dilakukan pada host lokal; salinan di luar host serta RPO/RTO cloud masih perlu penerimaan. |

## Bukti pengujian

- Suite PostgreSQL final: **346 tes lulus**, tanpa kegagalan atau error. Mencakup tambahan regresi impor, pajak dan kontrol transaksi bersamaan.
- Suite SQLite: **338 tes ditemukan; 337 lulus, 1 tes khusus PostgreSQL dilewati** pada run integrasi sebelumnya. Regresi parser tambahan diuji lagi secara terpisah; angka PostgreSQL di atas adalah run penuh terakhir.
- Finance PostgreSQL: **50 tes lulus**, termasuk perlombaan dua alokasi atas mutasi yang sama dan posting bersamaan dengan penutupan.
- Akun dan impor PostgreSQL: **18 tes lulus**.
- Browser Chrome: **148 pemeriksaan halaman** untuk tiga peran pada lebar 1440 dan 390, serta **10 alur interaksi**, tanpa kegagalan status, overflow seluruh halaman, atau error JavaScript. Pengujian memakai perusahaan sintetis di database terpisah.
- Uji browser tambahan: menu ponsel dapat dibuka/ditutup, nominal pecahan Rupiah tetap terlihat, QR Authenticator dapat didaftarkan, delapan kode pemulihan ditampilkan, kata sandi saja berhenti di tahap MFA, dan kode pemulihan dapat menyelesaikan login.
- Pemeriksaan migrasi, `pip check`, kompilasi Python dan pemeriksaan whitespace diff lulus. Pemeriksaan konfigurasi produksi, validasi Compose/Caddy serta uji backup/transfer dijalankan terpisah.

Hasil ini membuktikan kasus yang diuji. Angka tes bukan skor kesiapan perusahaan dan tidak membuktikan data nyata lengkap atau laporan pajak sudah diterima pemerintah.

## Penerapan pada aplikasi lokal

Delapan migrasi tambahan diterapkan setelah server lokal dihentikan dan backup database beserta dokumen dibuat serta diverifikasi. Dari 58 tabel sebelumnya, **55 tabel mempertahankan seluruh nilai kolom lama secara identik**; hanya tiga tabel metadata Django (`django_migrations`, `django_content_type`, `auth_permission`) berubah sebagaimana diharapkan. Pemeriksaan integritas SQLite lulus dan tidak ada pelanggaran foreign key.

Perusahaan tetap memiliki 0 invoice pelanggan, 0 mutasi bank dan 0 jurnal; belum ada sumber transaksi baru yang diberikan. Migrasi tidak mengaktifkan tarif, menyetujui rujukan pajak, menambah akun perusahaan, atau memasukkan data sintetis. Sebelas halaman perusahaan dirender dengan koneksi database yang dipaksa hanya-baca untuk memeriksa kompatibilitas data yang sudah ada.

Server lokal dimulai kembali menggunakan launcher yang dikelola. `/health/`, `/ready/`, dan halaman login merespons HTTP 200. Backup dan bukti perbandingan tetap privat dalam `.local/remediation-2026-09-09/`; source direkam dalam repositori Git lokal, tanpa publikasi remote.

Review persetujuan otomatis menolak peluncuran Docker Desktop dengan alasan umum “blocked by policy”, sehingga eksekusi container belum dibuktikan. Peluncuran server QA terpisah di latar belakang juga ditolak; uji browser berhasil dijalankan melalui server sementara di dalam proses pengujian yang berhenti setelah tes. Penolakan tersebut tidak menghalangi restart launcher aplikasi lokal yang sudah dikelola.

## Pekerjaan yang memerlukan sumber atau lingkungan nyata

1. E-statement 2026, rincian invoice/pembayaran, saldo awal lengkap, bukti layanan, dan dokumen pajak belum tersedia. Rekap yang telah diarsipkan tidak diubah menjadi transaksi rekaan. Pengecualian sumber harus diselesaikan dan hasil pembukuan dibandingkan dengan dokumen asli.
2. BNI masih mereview API. Implementasi autentikasi, transaksi terlambat/koreksi, sinkronisasi, dan uji total terhadap rekening koran menunggu kontrak API yang disetujui. Impor manual adalah jalur operasional yang tersedia.
3. Nama domain, akses server, DNS, penyimpanan backup di luar host, serta penerimaan HTTPS dan pemulihan cloud belum diberikan. Artefak deployment siap direview; layanan belum dipublikasikan.
4. Profil dan perhitungan pajak perusahaan memerlukan dokumen serta pemeriksaan profesional. Format resmi, lampiran lengkap, penyampaian dan penerimaan DJP belum disertifikasi melalui pengujian ini.
5. Kunci OpenRouter, persetujuan endpoint/model/provider, biaya dan evaluasi jawaban nyata belum tersedia. Konektor marketing langsung juga belum mendapat kredensial/cakupan. Input manual tetap dipisahkan dari data yang diverifikasi.
6. Revenue kursus per sesi, refund setelah layanan, pembayaran pemasok sebelum tagihan, aset/penyusutan lengkap dan migrasi saldo awal subledger masih merupakan pengembangan tambahan. Modul SDM/payroll/operation tidak diaktifkan oleh perbaikan ini.

**Keputusan penggunaan:** versi ini dapat dipakai untuk penerimaan pengguna dan penyiapan dokumen secara terkontrol. Belum dapat dinyatakan 10/10 sebagai sistem tunggal pembukuan dan pelaporan pajak otomatis perusahaan.

Panduan pekerjaan sehari-hari tersedia di [Panduan Finance](FINANCE-OPERATING-GUIDE.md), sedangkan konfigurasi, pemulihan dan cutover ada di [Panduan Cloud](../deploy/README.md).
