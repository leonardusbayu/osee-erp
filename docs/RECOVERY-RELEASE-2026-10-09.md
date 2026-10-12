# Pemulihan alur keuangan dan pekerjaan — 9 Oktober 2026

Batch ini menambahkan pembukuan pajak perusahaan, pelunasan invoice dengan bukti potong pelanggan, saldo bank bertanggal untuk Direktur, dan alih tugas marketing yang dapat diaudit. Batch ini sudah diuji dan diterapkan ke preview lokal. Tujuan ERP lengkap tetap aktif; hasil ini bukan sertifikasi seluruh perusahaan atau pelaporan DJP.

## Cara menggunakan

- **Potongan pelanggan:** dari invoice pilih “Siapkan potongan pelanggan”. Setelah kertas kerja dan bukti potong resmi diperiksa, cocokkan uang masuk bersih. Piutang dilunasi sebesar kas + kredit pajak; kas bank tetap nilai transfer sebenarnya.
- **Bukti potong datang kemudian:** pada kertas kerja pelanggan pilih “Catat bukti potong untuk pembayaran lama”. Pilih Allocation yang sudah terposting dan tanggal jurnal dalam periode terbuka. Sistem menutup sisa piutang sebagai kredit pajak, tanpa mengubah pembayaran/jurnal bank sebelumnya. Riwayat pada Penyelesaian keuangan membedakan uang bank lama dan kredit baru.
- **Pajak perusahaan:** dari kertas kerja final yang disetujui, catat utang lalu cocokkan transfer pajak yang sudah terjadi. Pembayaran parsial dibatasi sisa utang. Bank matching tidak membuktikan penerimaan DJP. PPh badan tahunan positif dari paket normal didukung dengan batas khusus di bawah.
- **Direktur:** saldo bank tampil pada cutoff akhir bulan selesai, setelah seluruh rekening mempunyai sumber kontinu yang diperiksa dan cocok dengan buku. Sumber berubah, mutasi belum diklasifikasikan atau saldo berbeda membuat angka belum tersedia. UI, CSV, PDF dan AI memakai fakta bertanggal yang sama. Pembagian nilai bank ke OpenRouter memerlukan persetujuan versi3 yang baru.
- **Marketing:** Owner/Direktur dapat memindahkan pekerjaan belum selesai ke staf aktif dengan akun perusahaan. PIC baru melanjutkan pekerjaan; PIC/delegasi lama kehilangan akses pelaksanaan. Pemilik awal, bukti dan riwayat pengalihan tetap tersimpan. KPI uang masuk memakai kas bank; kredit pajak tidak menambahnya. Sumber pelunasan bruto masuk histori/provenance KPI terkait.

## Hasil verifikasi

Source/test revision: `c7e66ae0a850b3f1e3a20752904b7c546bb51c5b7d692af26f1e80a032b03be9`.

| Pemeriksaan | Hasil |
|---|---|
| Suite Django SQLite | 625 ditemukan; 607 lulus, 18 kasus khusus PostgreSQL dilewati |
| Suite Django PostgreSQL17 | 625 lulus tanpa skip; termasuk 18 konkurensi |
| Peluncur Windows | 3 lulus: port asing, trailing argv asing, identitas PID berubah |
| Browser UX umum | 40 pemeriksaan halaman desktop/ponsel, 16 alur; hasil tersimpan dibaca kembali |
| Browser marketing | 26 pemeriksaan halaman, 6 alur; alih PIC/bukti/hasil KPI diverifikasi dari DB |
| Browser pajak/uang | 23 pemeriksaan halaman, 8 alur; kas neto, kredit terlambat, pembayaran parsial dan batas bukti resmi |
| Pemeriksaan independen terakhir | 5 HTTP probes lulus pada source tetap: histori kas/jurnal, periode laporan, replay, invoice salah dan tanggal ditolak |
| Kualitas source | Django check, migrasi tanpa drift, critical Ruff, compilation dan dependency check lulus |
| Gate | `READY_FOR_SCOPE` untuk batch terbatas; kebutuhan ERP lainnya tidak dinyatakan selesai |

Data sintetis dan penyimpanan terpisah digunakan untuk QA; provider dimatikan. Laporan awal yang gagal tetap disimpan. Pemeriksaan Ruff awal belum bisa berjalan karena paket QA tidak ada; hanya paket yang sudah tercantum di `deploy/quality-requirements.txt` dipasang, lalu pemeriksaan dijalankan. Tidak ada perubahan dependensi Django atau source karena instalasi tersebut. Kesalahan nama field pada alat perakitan laporan privat juga dicatat dan diperbaiki; itu bukan hasil pengujian aplikasi yang dihapus.

Bukti reproduksi lokal: `.local/quality-20261009/final-recovery-batch-2/report.md` dan `check.json`, dengan metadata, log, serta screenshot ber-hash. Database fixture browser dipertahankan privat untuk reproduksi; server fixture ditutup dan cluster PostgreSQL QA dihentikan.

## Penerapan lokal

Preview: [http://127.0.0.1:8001/modules/](http://127.0.0.1:8001/modules/). Port8000 dipakai aplikasi lain; peluncur memilih alamat tersedia dan mencatat proses milik workspace. Preview berjalan dengan proses baru yang dikelola `Start OSEE.cmd` / `Stop OSEE.cmd`.

Sebelum migrasi, preview dihentikan dan database SQLite serta dokumen privat dicadangkan dan diverifikasi. Catatan financial, tax obligation, organisasi/anggota, user dan pekerjaan yang sudah ada dibandingkan berdasarkan kolom asli, jumlah dan hash; seluruhnya tetap. Data demo tidak ditambahkan.

Migrasi yang diterapkan: Finance0010/0011, Taxes0008, Marketing0007/0008. Backfill pengalihan menetapkan PIC saat ini dari pemilik lama tanpa membuat kejadian pengalihan palsu. Referensi PER12 disiapkan sebagai versi belum terpublikasi; profil perusahaan dan publikasi aturan lama tetap sama. Health, readiness, login, redirect modul serta aset logo/CSS diperiksa setelah restart.

## Batas yang masih nyata

- Kredit terlambat saat ini memakai Allocation penerimaan invoice yang terposting. Bukti setelah penerapan uang muka pelanggan membutuhkan cabang `AdvanceApplication`; belum didukung.
- PPh badan tahunan di batch ini hanya kewajiban positif paket normal yang disetujui, tanpa angsuran PPh25. Karena tahun asal sudah ditutup, jurnal memerlukan PDF instruksi profesional, tanggal periode terbuka dan konfirmasi pengakuan terlambat. Laporan tahun lama tidak otomatis diperbaiki atau dibuka kembali.
- Pembetulan tahunan belum menghitung SPT lama, pembayaran dan pengembalian. Prepare/approve/verify menolaknya; sejarah dan unduhan tetap tersedia. Nihil/lebih bayar, angsuran dan koreksi/refund yang melibatkan kredit potongan masih membutuhkan alur lanjutan.
- Omzet pajak bulanan dan rekonsiliasi fiskal tahunan belum diturunkan otomatis lengkap per baris. Kertas kerja tetap draf internal; aplikasi tidak mengirim SPT atau transfer bank. Aturan perusahaan tidak ditentukan dari non-PKP atau omzet di bawah Rp4,8 miliar saja.
- Kejelasan untuk staf nyata perlu dibuktikan melalui UAT. Browser QA membuktikan tampilan dan alur teruji, bukan pemahaman setiap pengguna. Cloud/domain/HTTPS, sumber bank lengkap, akses BNI serta integrasi provider nyata belum diterapkan oleh batch ini.

[Daftar kebutuhan ERP yang tetap terbuka](FULL-APP-STATUS-2026-10-09.md) menjadi acuan pekerjaan berikutnya. Prioritas berikutnya adalah [paket sumber pajak bulanan](MONTHLY-TAX-SOURCE-PACKAGE-PLAN-2026-10-09.md), agar Finance tidak mengetik ulang total dan pengecualian yang sudah ada dalam transaksi.
