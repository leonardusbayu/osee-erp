# Realisasi anggaran dan hubungan tagihan Finance

Status: DIR-02/DIR-03 diterima untuk cakupan yang diuji dan diterapkan pada aplikasi lokal, 9 Oktober 2026. Gate `budget-final-2` menghasilkan **READY_FOR_SCOPE** untuk realisasi parsial dan hubungan eksplisit tagihan/pembayaran Finance. Aplikasi lokal berjalan di `http://127.0.0.1:8001`; buka [Anggaran](http://127.0.0.1:8001/director/budgets/). Hasil ini belum merupakan penerimaan seluruh ERP atau lingkungan cloud perusahaan.

## Alur untuk Finance dan Direktur

Finance membuka anggaran, memilih **Kelola belanja ini**, lalu mencatat biaya yang sudah terjadi sebagian, menghubungkan tagihan atau mencocokkan pembayaran yang telah dilaporkan. Halaman memperlihatkan biaya terjadi, komitmen tersisa, rencana tersisa, uang dilaporkan dan kas bank terverifikasi secara terpisah. Isian tetap tersedia ketika sumber berubah atau permintaan ditolak, dengan tindakan untuk memeriksa sumber kembali.

Contoh sintetis: komitmen Rp80 juta dengan pembayaran dilaporkan Rp30 juta, kemudian realisasi Rp20 juta, menghasilkan biaya terjadi Rp20 juta, komitmen Rp60 juta dan konsumsi anggaran Rp80 juta. Pembayaran tidak menghabiskan anggaran lagi. Tagihan prepaid Rp60 juta yang menggantikan rencana Rp80 juta, kemudian diakui Rp20 juta, menghasilkan biaya Rp20 juta, komitmen Rp40 juta dan rencana Rp20 juta.

Satu belanja dapat dihubungkan ke beberapa tagihan. Satu tagihan hanya mempunyai satu hubungan aktif dan harus dihubungkan seluruhnya; pembagian satu tagihan ke beberapa belanja belum didukung. Finance memilih catatan manual yang sudah mewakili tagihan agar biaya tidak dihitung dua kali. Pembayaran bank yang sudah ada dibaca tanpa membuat jurnal atau mutasi bank baru. Pembayaran manual yang cocok hanya dihitung sekali; sisanya tetap berlabel dilaporkan.

Owner/Direktur dapat melepas sisa yang belum digunakan dan memperbaiki perpindahan, hubungan tagihan atau pasangan pembayaran dengan alasan/bukti. Koreksi menyimpan riwayat lama dan catatan pengganti. Koreksi yang bertentangan dengan kapasitas yang sudah digunakan ditolak. Finance dapat menyiapkan hubungan dan realisasi; Marketing tidak dapat mengubah konsumsi anggaran. Auditor dapat membaca riwayat sesuai batas akses yang sudah tersedia.

## Angka dan bukti yang digunakan

Halaman, laporan PDF/CSV, API manajemen dan konteks AI menggunakan posisi anggaran kanonis yang sama, dengan tanggal cakupan dan fingerprint sumber. Kas terverifikasi, pembayaran dilaporkan, sisa utang pemasok dan sisa utang potongan pajak berbeda. Pembayaran neto pemasok serta penyetoran potongan yang benar-benar terjadi menjadi kas keluar; tidak menjadi biaya baru. `CostAllocation` tetap atribusi biaya.

Adapter memeriksa organisasi, identitas tagihan, seluruh baris jurnal, akun, tanggal, status, mutasi bank dan keseluruhan penggunaan sumber. Sumber hilang atau rusak menghasilkan tugas pemeriksaan dan nilai belum dapat dinilai. Nilai kosong tidak berubah menjadi nol. Biaya aktual di atas estimasi tetap terlihat sebagai kelebihan anggaran; aplikasi tidak menaikkan otorisasi otomatis. Pengurangan tagihan draft di bawah nilai hubungan memerlukan koreksi Owner dan tidak otomatis melepas selisih estimasi.

Tanggal bank yang berubah melewati cutoff tidak menyembunyikan pembayaran yang jurnal aslinya telah terposting sebelum cutoff. Sumber tetap diperiksa melalui tanggal bank atau jurnal tepat, lalu ketidakcocokan tanggal menolak bukti. Pembayaran yang bank dan jurnal aslinya sama-sama terjadi setelah cutoff tetap dikecualikan.

Mutasi bersifat atomik, memperoleh kunci organisasi lebih dahulu, dan memeriksa versi/fingerprint serta referensi permintaan. Retry identik mengembalikan hasil lama; isi berbeda dengan referensi yang sama ditolak. Jalur lama tidak dapat mengubah langsung catatan yang sudah dikelola sebagai belanja. Riwayat `BudgetEntry`, settlement dan fakta Finance tetap tersimpan.

Konteks AI Direktur sekarang memakai disclosure `director-aggregate-v4`. Owner perlu menyetujui ulang penggunaan agregat baru sebelum akses OpenRouter; persetujuan lama tidak mengizinkannya. Konteks keluar memakai agregat bertanggal yang dibentuk kode. Referensi sumber dan rincian privat tetap lokal. Perubahan fingerprint membuat review lama memerlukan penyegaran; jurnal dan riwayat tidak dihapus.

## Bukti penerimaan

Seluruh pemeriksaan final terikat pada source dan tes yang sama: `3bb3bda4d34e11760d3c609e7ca373a4d4cbba24a4d0c3ee11e4c2232277b2d5`. Fixture sintetis, database/media terpisah dan provider dimatikan; data perusahaan tidak menjadi fixture pengujian.

| Pemeriksaan | Hasil |
|---|---|
| SQLite seluruh aplikasi | 876 dijalankan: 839 lulus, 37 kasus khusus PostgreSQL dilewati |
| PostgreSQL17 seluruh aplikasi | 876 lulus, tanpa skip; termasuk 37 kasus khusus database |
| Kasus independen | 12 lulus, tanpa skip |
| Chrome desktop 1440px dan ponsel 390px | 10 alur, 132 keadaan halaman; tanpa error JavaScript atau overflow dokumen |
| Skema/kode/dependensi | Django check, migration drift, Ruff kritis E9/F63/F7/F82, kompilasi dan pip check lulus |

Lima race baru PostgreSQL memeriksa kapasitas konversi, retry identik, perebutan hubungan tagihan, koreksi bersamaan dengan konversi dan kapasitas pasangan pembayaran. Browser memeriksa delapan keadaan tersimpan (empat skenario pada masing-masing ukuran layar), termasuk realisasi parsial, prepaid, beberapa sumber manual, sumber berubah, koreksi Owner, batas peran/organisasi dan CSRF. Catatan asal, jurnal dan bank tetap sesuai ekspektasi; linking/retry tidak memutar ulang posting Finance. Review kode dan lima gambar final independen tidak menemukan blocker tambahan pada alur yang diperiksa. Ini belum membuktikan staf nyata memahami penggunaan pertama tanpa bantuan.

Gate mencatat **13 temuan produk terselesaikan**: alur/tombol realisasi yang hilang; penggunaan bank berlebihan; perubahan peran pemasok; organisasi tidak sah; cakupan form beberapa sumber; kegagalan halaman koreksi Owner; JSON rusak; pelepasan diam-diam setelah draft berkurang; penyaringan sumber organisasi lain; fingerprint sumber rusak yang tidak berubah; hilangnya pilihan sumber perbaikan; dan tanggal bank yang menyembunyikan pembayaran historis. Reproduksi gagal asli dan regresi tetap tersimpan.

Bukti berada di `.local/quality-20261009/budget-final-2/report.md`, `check.json`, `results.json` dan direktori `evidence`. Artefak browser asli berada di `.local/budget-spend-browser/run-kdgrk7m8`; paket gate hanya menyalin artefak yang diizinkan dan mengecualikan database serta berkas fixture berkredensial. Gate tidak menyatakan seluruh ERP bebas bug.

## Pembaruan database lokal

Backup `before-budget-link-20261009T134828Z.zip` diverifikasi untuk empat berkas dengan SHA256 `5a3394e683eff2aeb767cde226178a263efadfba7a7df186c7343e84b2719784`. Migrasi yang diterapkan hanya Director `0003_budget_spends`.

Perbandingan terhadap baseline dan database dalam arsip asli membuktikan **955 catatan asli pada 119 tabel tetap utuh**, beserta seluruh definisi schema lama. Angka 955 mencakup metadata framework lama, bukan jumlah transaksi perusahaan. Empat register baru `BudgetSpend`, `BudgetSpendMovement`, `BudgetBillLink` dan `BudgetPaymentMatch` masih kosong. Tidak ada transaksi, hubungan belanja, pengguna, persetujuan atau keputusan pajak perusahaan yang dibuat otomatis.

Django menambahkan tepat empat definisi content type dan 16 definisi izin CRUD untuk register baru, serta satu catatan riwayat migrasi. Definisi izin tersebut tidak memberikan izin baru kepada pengguna atau grup; grant dan membership lama tetap sama. Integritas database/FK dan fakta pajak perusahaan tetap terjaga. Receipt: `.local/quality-20261009/budget-company-preservation-20261009T134828Z.json`.

Guard awal menghentikan refresh sebelum restart karena penambahan 20 metadata normal itu tidak memenuhi perbandingan identik semula. Recovery menggunakan arsip dan baseline asli yang sama, allowlist metadata tepat serta pembandingan schema lengkap; tidak membuat baseline pengganti, restore atau migrasi kedua. Bukti percobaan awal tetap disimpan. Log startup berikutnya menyatakan tidak ada migrasi tertunda dan pengisian demo dilewati karena data perusahaan tersedia.

Sesudah startup, **11 pemeriksaan HTTP anonim lulus** pada source yang sama: health/readiness/login dan tiga aset menghasilkan status 200; modul, anggaran, detail belanja, laporan Direktur dan `/bills/` mengarah ke login dengan status 302. Pemeriksaan tidak masuk ke akun atau membaca catatan perusahaan terautentikasi. Data setelah startup kembali dibandingkan terhadap arsip/baseline asli: 119 tabel/955 catatan, schema lama, integritas/FK, grant dan membership tetap utuh; hanya 20 metadata yang diizinkan bertambah dan empat register baru tetap kosong. Bukti: `.local/quality-20261009/budget-preview-runtime-20261009.json`.

Identitas server PID 81596, peluncur PID 89560 dan listener 8001 sesuai workspace. PostgreSQL QA 15445 telah dihentikan; proses aplikasi lain pada 8000 dan PostgreSQL sistem 5432 tetap hidup. Bukti kepemilikan: `.local/quality-20261009/budget-runtime-ownership-20261009.json`. Server lokal ini tetap lingkungan pengembangan, bukan deployment cloud/HTTPS perusahaan.

## Percobaan gagal yang tetap disimpan

`budget-final-1` tetap berstatus `NOT_READY`: satu oracle tes bank masih mengharapkan consent v3 setelah disclosure v4 ditambahkan. Pembetulan hanya mengubah literal versi yang tepat dan komentar; pemeriksaan cutoff, agregat bank, privasi serta penolakan consent lama tetap ada. Build final baru menjalankan ulang seluruh suite. Kesalahan registrasi command dan pemanggilan nama kelas tes yang tidak ada juga tetap tercatat sebagai kesalahan runner/loader; tidak dihitung sebagai kasus lulus. Kegagalan fixture/oracle komponen sebelumnya tetap dibedakan dari 13 cacat produk.

Probe runtime pertama keliru memakai `/finance/bills/`, yang menghasilkan 404. Rute terdaftar yang benar adalah `/bills/` dan lulus pada pemeriksaan berikutnya. Percobaan asli tetap disimpan sebagai `probe_route_error` di `.local/quality-20261009/budget-preview-runtime-attempt1.json`; pemeriksaan database setelah startup belum dijalankan pada percobaan itu. Ini bukan temuan cacat produk atau kasus HTTP yang lulus.

## Batas dan pekerjaan berikutnya

[MKT-03, pemulihan identitas sumber iklan](MARKETING-AD-RECOVERY-PLAN-2026-10-09.md), merupakan batch kode berikutnya. [Daftar seluruh kebutuhan](FULL-APP-STATUS-2026-10-09.md) tetap aktif. Uang muka/refund pemasok, split-Bill, forecast, revisi otorisasi anggaran dan paket rapat permanen serta pajak/penggajian lanjutan masih terbuka. Cutoff belanja belum merekonstruksi versi persetujuan anggaran historis.

Kelengkapan dokumen perusahaan, keputusan pajak profesional, UAT staf, cloud HTTPS, pemulihan eksternal dan sertifikasi provider live belum dibuktikan oleh pengujian sintetis ini. Rilis ini tidak mengirim laporan ke DJP atau menyatakan seluruh aplikasi siap digunakan perusahaan.
