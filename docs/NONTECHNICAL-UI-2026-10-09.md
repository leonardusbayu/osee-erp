# Kelanjutan tampilan sederhana untuk staf — 9 Oktober 2026

Melanjutkan standar [tampilan staf nonteknikal](NONTECHNICAL-UI-2026-10-03.md).
Tujuannya membantu staf mengenali pekerjaan, melihat tindakan berikutnya,
dan memperbaiki kesalahan tanpa mengulang isian. Pengujian otomatis belum
membuktikan bahwa semua staf memahami aplikasi saat pertama melihatnya.

## Finance

- Pesanan biasa menampilkan pelanggan, produk, jumlah dan tanggal dahulu.
  Harga khusus berada di bagian tambahan dan terbuka bila isian ditolak.
  Harga yang berbeda tetap membutuhkan persetujuan Direktur yang cocok
  dengan pelanggan, produk, jumlah dan tanggal, serta hanya berlaku sekali.
- Kesalahan pencocokan mutasi tetap menampilkan dokumen pilihan, nominal,
  filter dan halaman daftar. Baris terkait terbuka dengan pesan di dekat
  formulir. Kegagalan tidak mencatat pembayaran.
- Jika belum ada dokumen yang memenuhi syarat, tersedia penjelasan dan
  tautan menuju invoice, pemeriksaan tagihan atau pencatatan uang muka.
  Persyaratan terbitnya invoice dan pemeriksaan pajak tidak dilonggarkan.
- Di ponsel, mutasi menjadi kartu dengan label dan tindakan di bawahnya;
  staf tidak perlu menggeser tabel ke kanan untuk mencocokkan pembayaran.
- Pilihan kolom e-statement dikelompokkan berdasarkan tanggal, uang dan
  identitas transaksi. Rentang baris serta mata uang berada di bagian
  tambahan yang terbuka saat ada kesalahan. File kosong hanya meminta
  pernyataan eksplisit, tetap melalui pratinjau dan konfirmasi terpisah.
  File berisi transaksi tidak boleh dinyatakan sebagai periode kosong.

## Marketing

- Pekerjaan mendesak pada antrean lain mempunyai tautan yang terlihat.
  Daftar hari ini yang kosong tidak menyembunyikan janji terlambat atau
  pekerjaan yang belum ditangani. Jumlah antrean tidak dijumlahkan karena
  satu pekerjaan dapat muncul pada beberapa jenis antrean.
- Janji berikutnya menampilkan tanggal dan jam asli. Mengedit catatan atau
  mempertahankan tanggal tidak menghapus jam tersebut. Mengganti atau
  menghapus tanggal tetap menghapus jam lama yang sudah tidak sesuai.
- Formulir mempertahankan ketelitian waktu respons yang tersimpan ketika
  nilai tampilan yang tidak diubah dikirim kembali. Perubahan waktu yang
  sebenarnya tetap memerlukan alasan dan bukti koreksi.
- Kolom kontak menjelaskan kapan alasan tidak lanjut dan jadwal baru
  wajib diisi. Catatan yang tidak memenuhi syarat tidak disimpan.
- Tombol kontak pada riwayat mengikuti penugasan atau delegasi aktif;
  penolakan di server tetap berlaku. Nama kolom metode kontak tidak lagi
  mengganggu penanganan tombol simpan oleh JavaScript.

## Pemeriksaan dan batasnya

Pengujian memakai database sintetis terpisah, tanpa konfigurasi atau kunci
provider perusahaan. Tidak menambahkan staf, transaksi atau target pada
data asli. Reproduksi gagal tetap disimpan, termasuk kegagalan membaca
kembali catatan dari database dan kesalahan JavaScript pada kontak.

Browser Chrome memeriksa 40 halaman desktop/ponsel dan 16 alur, termasuk
penolakan alokasi berlebih, pernyataan file kosong, konfirmasi impor yang
terpisah, navigasi keyboard, batas akses dan ketepatan janji setelah simpan.
Ada pembacaan kembali database untuk memastikan catatan tersimpan dan
pembayaran tidak tercatat dari percobaan yang ditolak. Tangkapan layar
ponsel diperiksa untuk pencocokan mutasi, janji dan pratinjau file kosong.

Suite SQLite menemukan 564 tes: 551 lulus dan 13 tes khusus konkurensi
PostgreSQL dilewati. Seluruh 24 tes baru untuk alur ini lulus. Pemeriksaan
migrasi tidak menemukan perubahan skema. Pemeriksaan akhir menggunakan
identitas sumber yang sama sebelum dan sesudah pengujian:
`25316c1fa1dd70fc087724947ac0bc26005fb85d68c99e69b56b4dc71ab1ac1e`.
Paket bukti terikat di `.local/simple-ux-20261009/final-gate-2/` lulus untuk
cakupan terpilih ini; hasil itu tidak merupakan sertifikasi seluruh ERP.

Bukti privat: `.local/simple-ux-20261009/`,
`.local/fix-2026-10-02/verification/*20261009*`, dan
`.local/simple-ui-qa/run-k4me1qih/`. Pemeriksaan independen atas harga,
pemetaan bank, waktu dan batas penugasan tidak menemukan masalah tambahan.

Cloud/HTTPS, integrasi BNI, AI provider, ketepatan aturan pajak terkini dan
pengajuan resmi tidak dinilai pada pekerjaan tampilan ini. Perhitungan dan
kontrol keuangan tetap menggunakan layanan yang sudah ada. Penyaringan
pilihan dokumen bank berdasarkan tanggal tiap mutasi masih dapat diperbaiki;
server sudah menolak pencocokan yang mendahului tanggal dokumen.

## Percobaan singkat dengan staf asli

Berikan tugas berikut tanpa menjelaskan urutan klik. Amati apakah staf bisa
menemukan tindakan berikutnya, menjelaskan akibatnya, dan memperbaiki satu
kesalahan. Catat bagian yang membutuhkan bantuan; jangan menjadikannya
kesalahan staf.

1. Finance: buat draf pesanan dengan harga biasa, kemudian cari cara meminta
   harga khusus tanpa menerbitkan invoice yang belum diperiksa.
2. Finance: unggah e-statement dan temukan perbedaan antara pratinjau dan
   impor; perbaiki pilihan kolom yang salah sebelum konfirmasi.
3. Finance: cocokkan sebagian pembayaran; saat nominal ditolak, perbaiki
   tanpa mencari ulang transaksi atau dokumen.
4. Marketing: buka janji terlambat dari halaman harian, catat hasil kontak,
   tetapkan janji baru, lalu ubah catatan tanpa mengubah jam janji.
5. Direktur: temukan keputusan yang perlu disetujui dan jelaskan data yang
   masih belum lengkap sebelum mengambil keputusan.

Percobaan staf asli ini belum dilakukan. Standar tampilan sederhana tetap
berlaku untuk modul berikutnya.
