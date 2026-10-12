# Koreksi jumlah piutang dan utang lama — rilis 10 Oktober 2026

**Status: `ACCEPTED_FOR_SCOPE`, terpasang di aplikasi lokal perusahaan. `all_erp_ready: false`.** Rilis ini menerima koreksi jumlah pokok saldo awal dengan bukti pengujian dan pemasangan aktual; penerimaan seluruh ERP, UAT staf, dan deployment cloud masih terbuka.

Source teruji dan terpasang: `a42fae989bd3d21507a5e044cdd9f936e68ddf65543fa7a5b24f1e1dd20ba850`. Baseline sebelum pemasangan: `bf4a8d16ce3ecf07fb406786308a3735f9ca2feb7bc8e0d9b7902d26a438f9c7`. Identitas source mengecualikan dokumen. [Rencana koreksi](OPENING-PRINCIPAL-CORRECTION-PLAN-2026-10-10.md) dan [rilis saldo awal sebelumnya](OPENING-AR-AP-RELEASE-2026-10-10.md) tetap menjadi riwayat.

## Perubahan dan cara memakai

Owner aktif dapat menambah atau mengurangi jumlah piutang pelanggan/mitra dan utang pemasok lama. Setiap koreksi menyimpan tanggal berlaku, jumlah perubahan positif, alasan, bukti, pencatat, dan jurnal sendiri. Jumlah asli, pihak, nomor dan tanggal dokumen, tanggal awal pembukuan, serta bukti asal tetap tersimpan.

1. Buka **Finance → Piutang & utang lama → Lihat rincian → Perbaiki jumlah**.
2. Pilih **Tambah** atau **Kurangi**. Isi jumlah perbaikannya, tanggal berlaku, alasan, dan dokumen pendukung.
3. Tekan **Periksa sumber**. Bandingkan jumlah sebelum/sesudah dan sisa belum dibayar dengan dokumen, lalu centang konfirmasi dan tekan **Konfirmasi dan simpan**.

Finance membaca hasil dan riwayat koreksi. Pembayaran baru memakai **Cocokkan pembayaran** dengan mutasi bank nyata. Marketing tidak dapat mengakses alur ini. Peran pembaca keuangan, termasuk `director` yang hanya membaca, tidak otomatis mendapat hak koreksi Owner. Pemeriksaan berlaku pada halaman dan penyimpanan, termasuk penolakan sumber perusahaan lain.

Jika sumber berubah saat formulir terbuka, penyimpanan ditolak dan isian tetap tersedia. Tekan **Periksa sumber** kembali, periksa ringkasan baru, lalu konfirmasi kembali. Identitas permintaan diperbarui dan konfirmasi lama tidak dipakai. Histori memakai label **Jumlah ditambah**, **Jumlah dikurangi**, dan **Pembayaran dicocokkan**; tampilan tanggal historis hanya untuk membaca.

## Kontrol pembukuan dan sumber pajak

Koreksi mengubah pokok dan ekuitas saldo awal. Koreksi tidak membuat uang bank, invoice, tagihan baru, penjualan, biaya, atau keputusan pajak. Pembayaran sebelumnya tetap merupakan peristiwa kas tersendiri.

Tanggal harus sejak tanggal awal pembukuan sampai hari ini dan berada pada periode terbuka. Pokok efektif dan sisa tidak boleh negatif pada setiap tanggal kejadian, termasuk terhadap pembayaran berikutnya. Pembayaran lebih awal tidak dapat memakai tambahan pokok yang baru berlaku kemudian. Permintaan ulang identik tidak menambah jurnal kedua. Isian ganda/tambahan, penyimpanan tanpa konfirmasi atau CSRF, serta sumber rusak ditolak. Sumber yang rusak tetap belum dapat dinilai; tidak ditampilkan sebagai nol.

Sumber yang dipakai laporan bulanan disetujui atau laporan tahunan disetujui/dilaporkan dilindungi. Jika revisi bulanan memang mengizinkan pelepasan, persetujuan lama harus dilepas **secara eksplisit saat sumber masih sesuai, sebelum koreksi**. Revisi draf saja tidak cukup. Kewajiban berlanjut, paket tahunan, dan buku tertutup dapat tetap menahan perubahan. Tidak ada penarikan kewajiban, pembukaan buku, atau pelepasan persetujuan otomatis. Pesan penolakan mengarahkan ke laporan bulanan atau SPT tahunan sesuai alasan.

Koreksi pokok tidak menentukan bruto, pemotongan, kredit pajak, omzet, atau kewajiban baru. Pemeriksaan profesional pembayaran asli tetap terpisah. Cabang pemotongan positif belum diterima dalam rilis ini. Informasi non-PKP, PT biasa/non-perorangan, dan omzet UMKM tidak dipakai untuk mengarang kelayakan rezim pajak. Dokumen ini tidak menyatakan hukum atau kepatuhan pajak perusahaan telah divalidasi.

## Bukti pengujian aktual

| Pemeriksaan | Hasil |
|---|---|
| SQLite seluruh aplikasi | 1.176 kasus: **1.113 lulus / 63 skip**, exit 0, teardown normal. Skip khusus PostgreSQL bukan pass. |
| SQLite terkait | 108 kasus: **99 lulus / 9 skip**, exit 0, teardown normal. |
| PostgreSQL terkait | **108 lulus / 0 skip**, termasuk 9 kasus transaksi bersamaan; exit 0, teardown normal dan smart stop normal. PostgreSQL lain tidak disentuh. |
| Oracle publik asli | **1 lulus**, oracle tetap sama dengan RED asli. Regresi orphan fiskal juga mempertahankan metode asli yang pernah gagal. |
| Quality | Django check, deteksi migrasi, Ruff fatal checks, kompilasi 12 file Python berubah, dan pip check lulus. |
| Browser | **8 alur / 36 keadaan**, seluruh 4 alur ponsel tanpa JavaScript; 0 error, overflow horizontal, atau akses eksternal. |
| Hasil tersimpan dan visual | **8 baca ulang finansial native lulus**; root memeriksa desktop dan ponsel. UAT staf belum dilakukan. |

Semua hasil diikat pada source yang sama dan memakai data sintetis terpisah. Hasil ini bukan bukti kelengkapan data perusahaan atau koneksi provider nyata. Bukti kegagalan awal pemeriksaan waktu PostgreSQL dan klasifikasi proses alat tetap disimpan; perbaikan operasional tidak mengubah source produk atau oracle yang sudah diuji.

## Pemasangan perusahaan aktual

Pemasangan 16 file source selesai pada 10 Oktober 2026 WIB. Pemeriksaan setelah restart membuktikan isi lama **133 tabel / 1.025 baris**, kolom, urutan bisnis, timestamp migrasi lama, dan seluruh unggahan tetap utuh. Migrasi hanya `finance.0015_opening_balance_corrections`: satu tabel bisnis baru kosong, lima definisi framework, dan satu metadata migrasi. **Nol baris transaksi bisnis ditambah atau diubah oleh pemasangan.** Dua belas pemeriksaan runtime mencakup health, readiness, login, perlindungan halaman, dan stylesheet yang cocok dengan source.

Backup database/media terverifikasi: `.local/backups/before-opening-principal-corrections-20261009T225905Z.zip`. Backup source: `.local/backups/before-opening-principal-corrections-source-20261009T225905Z.zip`. Hash keduanya terikat pada receipt pemasangan. Penerimaan native asli tetap immutable di `.local/quality-20261010/correction-acceptance-1.json`; status pemasangan aktual dicatat terpisah di `.local/quality-20261010/correction-rollout-accepted-1.json` pada workspace perusahaan.

Aplikasi lokal: [Piutang & utang lama](http://127.0.0.1:8001/finance/opening-balances/). Pengguna tetap perlu login dengan akun perusahaan.

## Pekerjaan berikutnya

Pembatalan pencocokan bank yang salah dan refund uang nyata memerlukan dua alur berbeda. Berikutnya: kas saldo lama dengan pemotongan, akun saldo awal lain/aset dan kewajiban pajak lama, UAT staf, sumber keuangan nyata terverifikasi, akses BNI resmi yang masih dalam review, serta cloud/domain/HTTPS. [Status seluruh ERP](FULL-APP-STATUS-CORRECTIONS-2026-10-10.md) tetap menyatakan `all_erp_ready: false`.
