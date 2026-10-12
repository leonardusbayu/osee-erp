# Instalasi Marketing: dependensi Direktur diperbaiki

INF-01 selesai untuk source `ad9f997413b34f98afce8591017ca930406d839de56ab73cdb95b31ab3893c12`. Gate `marketing-dependency-final-3` menghasilkan **READY_FOR_SCOPE**: empat kasus terpenuhi dan tiga temuan terselesaikan. Cakupan ini adalah dependensi instalasi Marketing, bukan seluruh kesiapan ERP. [Daftar kerja aplikasi](FULL-APP-STATUS-2026-10-09.md) tetap terbuka.

Sebelumnya, instalasi yang menargetkan Marketing0008 atau Marketing0009 pada database kosong gagal menyelesaikan `director.Decision`. Marketing0005 membuat relasi ke Decision/Budget tanpa mendeklarasikan migrasi pembentuknya. Migrasi seluruh aplikasi kebetulan menerapkan Direktur lebih dahulu, sehingga pengujian penuh saja tidak menemukan celah tersebut.

Perbaikan menambahkan `director.0001_initial` ke dependency Marketing0005. Seluruh operasi migrasi dan byte lain pada file tersebut tetap sama. Dua tes permanen membangun registry historis pada Marketing0008/0009 dan memeriksa bahwa kedua relasi menunjuk kelas Direktur yang sebenarnya. Graph tidak memiliki cycle. Pembuktian source merekonstruksi checkpoint MKT-03 `e32c5d84aa804d04554c77f66b817a72e14a61e265d3ec1162c51513473eeedd` dengan membalik hanya dependency tersebut dan mengeluarkan file tes baru: tidak ada perubahan model bisnis, UI, Finance atau provider dalam batch ini.

## Bukti pengujian

| Pemeriksaan | Hasil nyata | Batas |
|---|---|---|
| Reproduksi awal SQLite | Dua target Marketing-only gagal pada e32; cleanup normal | Kegagalan asli dipertahankan |
| Tes state sebelum perbaikan edge | Dua error resolusi model pada source yang baru menambahkan tes | Berbeda dari source e32 asli |
| SQLite sesudah perbaikan | Dua perintah migrasi nyata pada database kosong terpisah berhasil; suite penuh menemukan 926 kasus, 884 lulus dan 42 kasus khusus PostgreSQL dilewati | Bukan 926 kasus yang dieksekusi/lulus |
| PostgreSQL sesudah perbaikan | Dua target migrasi pada database kosong terpisah berhasil; histori tepat 16/17 node mencakup Direktur; 23 tes terkait lulus tanpa skip; database tes dihancurkan normal | Suite penuh 926 kasus PostgreSQL tidak diulang pada ad9 |
| Pemeriksaan independen | Dua tes state lulus, pembuktian dua file source dan differential guard PostgreSQL lulus | Tidak menguji alur bisnis ERP lain |
| Kualitas akhir | Lima pemeriksaan baru dengan interpreter `.venv`: Django, drift migrasi, Ruff kesalahan utama, kompilasi, dependensi; semuanya lulus | Bukan audit seluruh kerentanan dependensi |

Suite SQLite penuh dijalankan sekali pada ad9 dalam native `marketing-dependency-final-sqlite-20261010-1`; PostgreSQL nyata dan 23 tes terkait dijalankan dalam `marketing-dependency-final-postgres-20261010-2`. Gate ketiga memverifikasi ulang bukti sukses yang tidak berubah melalui tiga pemeriksaan filesystem dengan registrasi baru, lalu menjalankan lima pemeriksaan kualitas baru. Registrasi asli, waktu, source sebelum/sesudah, jumlah tes, hash fixture, log, hasil perintah migrasi dan cleanup diperiksa. **Gate ketiga tidak menjalankan kembali suite atau perintah migrasi tersebut.**

Binding gate akhir memakai environment `isolated-filesystem-reuse-explicit-venv-quality-INF01-20261010` dan test revision source ad9 ditambah private PostgreSQL guard SHA256 `6a8834591829c70b1de17beac7d51bdb70592b08076c9789d7d756d7cc6f7a44`. Plan tersegel berhash `ba2ab06b9547e9f8832a4afa9135665938403cd084888fd141977cb6d262bb50`. Review independen dibekukan sebelum pengumpulan akhir; hash review `f98dc1db25ec27e19d0de47fbdadaf6f03ba6699e16b28c6af6b620d0d06bcf7`.

## Kegagalan pemeriksaan yang tetap tercatat

Gate pertama tetap **NOT_READY**. Guard private PostgreSQL membandingkan alamat `127.0.0.1/32` dengan `127.0.0.1`, lalu menolak sebelum membuat database atau menjalankan migrasi. Perbaikannya hanya mengganti representasi SQL menjadi `host(inet_server_addr())`; pembalikan byte merekonstruksi helper lama. Differential read-only pada server yang sama membuktikan penolakan guard lama dan penerimaan guard baru, dengan pemeriksaan PID, executable, command, data directory, start epoch, port, role dan database tetap sama. Gate pertama juga mempertahankan kesalahan pengemasan referensi log kosong. File kosong tetap diinventarisasi dan dihash, tetapi tidak digunakan sebagai bukti yang memenuhi gate.

Gate kedua tetap **NEEDS_EVIDENCE**, dengan tiga kasus lulus dan kualitas gagal. Perintah root memakai `python` di luar lingkungan aplikasi; Django tidak ditemukan sebelum pemeriksaan pertama selesai. Exit1, registrasi dan log asli tidak diganti. Gate ketiga memakai interpreter `.venv` secara eksplisit, mempertahankan temuan lingkungan itu, lalu menyelesaikannya berdasarkan lima pemeriksaan baru yang lulus. Persiapan schema gate kedua sempat ditolak sebelum seal karena dua field metadata yang tidak didukung; record persiapan juga disimpan, tanpa eksekusi native pada tahap itu.

Cluster QA milik batch pada loopback15445/PID51356 dihentikan setelah database tes dan dua database sementara hilang normal, serta tidak ada client lain. PostgreSQL sistem pada5432/PID7860 tetap sama. Tidak ada force-drop database atau penghentian sesi asing.

## Aplikasi lokal dan preservasi perusahaan

Aplikasi dinyalakan pada [OSEE lokal](http://127.0.0.1:8001/modules/) dengan proses terdaftar PID42876, parent51636, mulai `2026-10-09T17:38:14.5892054Z` (10 Oktober WIB). Startup mencatat **No migrations to apply** dan pengisian demo dilewati karena data perusahaan tersedia.

Pemeriksaan sesudah startup pada `2026-10-09T17:38:26.695193+00:00` lulus: 15 pemeriksaan HTTP anonim, termasuk login/health/readiness, logo dan aset UI, serta redirect autentikasi pada rute Marketing/Direktur/modul. Histori migrasi diperiksa menggunakan SQLite `mode=ro` dan `query_only`: 61 migrasi tersimpan konsisten, 12 graph leaf, tidak ada migrasi tertunda dan tidak ada migrasi yang dieksekusi oleh pemeriksa.

Preservasi membandingkan kembali arsip, baseline dan receipt **asli** MKT-03, tanpa membuat baseline pengganti. Seluruh 975 catatan asli pada 123 tabel, proyeksi kolom/metadata, schema yang tidak terkait, dokumen, grant/anggota, fakta pajak, integritas dan foreign key tetap sama. Register pemulihan iklan tetap kosong dan tiga kolom tambahan pada data iklan asli tetap null. Lima definisi framework dan satu catatan migrasi merupakan tambahan dari aplikasi MKT-03 sebelumnya; INF-01 tidak menambahkan catatan bisnis atau migrasi baru.

Arsip asli SHA256 `6fbe09c8195364c48e2ba5e2e18022123288aad6dee1d8d3454f60a471a4bdf3`; receipt preservasi asli SHA256 `68063c837c814efa90fcac53e1036a018e0180ef6e396817ca929f47cd319b1b`. Receipt runtime baru berada lokal di `.local/quality-20261009/marketing-dependency-preview-runtime-original-preservation-20261010-1.json`. Verifikasi tidak login ke akun perusahaan, mengubah data bisnis atau menghubungi provider.

## Pekerjaan berikutnya

[MKT-02: koreksi pelanggan/produk dari invoice keliru](MARKETING-INVOICE-IDENTITY-RECOVERY-PLAN-2026-10-10.md) masih belum diimplementasikan. Dua reproduksi publik Owner/Finance yang mengembalikan404 tetap menjadi oracle awal. Pemulihan harus menjaga nilai manual/kampanye, asal inferensi yang dapat ditelusuri, riwayat, batas peran, data Finance/Operations serta kesegaran KPI/AI.

Bukti browser dan suite PostgreSQL penuh MKT-03 tetap bukti historis e32. Batch ini tidak mengklaim browser atau suite tersebut diuji ulang pada ad9. Backlog Finance/Tax/Director/Operations/HR, kelengkapan data perusahaan, UAT pertama kali staf, cloud/domain/HTTPS dan koneksi live masih perlu ditangani.
