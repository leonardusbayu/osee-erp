# Bukti potong setelah penggunaan uang muka pelanggan

Status: FIN-05 diterima untuk cakupan ini dan diterapkan pada aplikasi lokal, 9 Oktober 2026. Aplikasi berjalan di `http://127.0.0.1:8001`; halaman keuangan berada di `/finance/movements/`. Ini belum merupakan penerimaan seluruh ERP atau lingkungan cloud perusahaan.

## Perubahan bagi Finance

Finance dapat memilih **Pembayaran yang sudah tercatat** ketika bukti potong PPh23 diterima setelah uang muka digunakan untuk invoice. Pilihan mencakup alokasi pembayaran bank lama dan penggunaan uang muka yang tepat. Finance memeriksa bukti, tanggal pembukuan dan ringkasan, lalu mencatat kredit sekali.

Contoh pengujian: uang bank Rp490.000 digunakan untuk invoice Rp500.000. Bukti potong yang sudah dicocokkan sebesar Rp10.000 melunasi sisa piutang. Sistem menambahkan kredit pajak Rp10.000; uang bank tetap Rp490.000 dan kas baru Rp0. Jurnal uang muka serta penggunaannya tetap utuh.

Formulir dan riwayat menampilkan uang bank asli, uang muka yang digunakan, serta kredit pajak baru secara terpisah. Kesalahan mempertahankan isian. Bukti yang belum memenuhi pemeriksaan menunjukkan langkah berikutnya. Hasil yang tersimpan dapat dibaca tanpa tombol pencatatan ulang; pengulangan identik mengembalikan hasil lama, sedangkan isi berbeda ditolak.

## Perlindungan pembukuan dan sumber

- Settlement memiliki tepat satu sumber: bank, Allocation atau AdvanceApplication. Sumber dan riwayat terposting dilindungi; cabang kredit terlambat mempunyai kas nol.
- Organisasi, pelanggan, invoice, bank/posting dan seluruh baris dua jurnal uang muka diperiksa. Sumber hilang atau berubah menolak pencatatan baru secara atomik.
- Identitas, perlakuan, tanggal terutang, kertas kerja dan bukti potong memakai pemeriksaan TAX-02 yang sama. Aplikasi tidak menetapkan tarif, tanggal pajak atau gross-up dari nominal bank.
- Kredit hanya menambah piutang pajak dan mengurangi piutang pelanggan. Tidak membuat transaksi bank, alokasi atau penggunaan uang muka baru. Periode sumber lama dapat tetap tertutup; jurnal kredit baru memerlukan tanggal dan periode yang sah.
- Finance/Owner aktif dapat mencatat; Marketing tidak mendapat akses mutasi. Batas organisasi, permintaan palsu, perubahan retry dan persaingan penggunaan bukti diperiksa di server/database.

Paket sumber bulanan versi2, provenance KPI Marketing versi5 dan manifest komersial Operations versi2 sekarang mencakup bukti penggunaan uang muka. Kredit kas nol tidak menjadi penerimaan bank baru. Perubahan versi ini dapat membuat paket/evaluasi lama memerlukan penyegaran dan review sebelum digunakan kembali; snapshot historis dan jurnal tetap tersimpan. KPI lead yang menerima pembayaran sebagian tetap mengikuti definisi lamanya, sedangkan status order lunas dapat berubah setelah kredit yang sah.

## Bukti penerimaan

Seluruh pengujian final memakai source yang sama: `1dda23a27a7b4ffb866740c1c050e91e943097e5969ef2491524c39367d9f5bf`. Provider dimatikan dan data sintetis terpisah digunakan.

| Pemeriksaan | Hasil |
|---|---|
| SQLite seluruh aplikasi | 807 dijalankan:775 lulus,32 kasus khusus PostgreSQL dilewati |
| PostgreSQL17 seluruh aplikasi | 807 lulus, tanpa skip; termasuk32 kasus khusus database dan5 race baru FIN-05 |
| Kasus penolakan/pemulihan independen | 14 lulus |
| Chrome desktop1440px dan ponsel390px | 4 alur/24 keadaan halaman; tanpa error JavaScript atau overflow dokumen |
| Skema/kode/dependensi | Django check, migration drift, Ruff kritis, kompilasi dan pip check lulus |

Browser memverifikasi data tersimpan untuk kedua ukuran layar: kas asli dan jurnal tidak berubah, kredit Rp10.000, invoice lunas Rp500.000, satu hasil setelah retry, periode lama tetap tertutup, serta hubungan bukti tepat. Review independen menerima kontrak integrasi dan halaman yang diperiksa. Ini tidak menggantikan pengamatan staf nyata pada penggunaan pertama.

Gate `.local/quality-20261009/advance-final-1` menghasilkan `READY_FOR_SCOPE`: dua celah awal FIN05-PROOF-01 dan FIN05-UI-01 diselesaikan, tidak ada blocker pada cakupan yang diperiksa. Kegagalan fixture/oracle komponen sebelumnya tetap disimpan dan dibedakan dari cacat produk. Bukti gate bukan jaminan tidak ada bug di seluruh ERP.

## Pembaruan aplikasi lokal

Server OSEE milik workspace dihentikan sebelum pembaruan. Backup `before-advance-credit-20261009T114823Z.zip` diverifikasi untuk empat berkas dengan SHA256 `cdcc04c0f72833c00b341bb586d65c45c7c8b15e5883bffc5b8d8b28d987d8b4`. Preflight membatasi migrasi ke Finance `0012_customer_tax_advance_credit`; tidak menjalankan perubahan lain yang tertunda.

Perbandingan seluruh nilai kolom asli membuktikan **955 catatan pada119 tabel tetap sama**. Tidak ada tabel bisnis tambahan, transaksi, settlement, identitas atau keputusan pajak baru. FK baru pada catatan yang ada tetap kosong. Bukti preservasi berada di `.local/quality-20261009/advance-company-preservation-20261009T114823Z.json`.

Peluncur menjalankan ulang OSEE pada8001, PID59828, dengan executable/command/listener sesuai state. Sembilan pemeriksaan HTTP readiness, login, perlindungan rute dan aset merek lulus pada source yang sama. Pemeriksaan HTTP tidak masuk ke akun perusahaan atau mengubah transaksi. PostgreSQL QA15445 telah dihentikan; proses aplikasi lain pada8000 dan PostgreSQL sistem5432 tetap hidup.

## Pekerjaan berikutnya

[Hubungan budget dengan tagihan Finance](BUDGET-FINANCE-LINK-PLAN-2026-10-09.md) merupakan tahap berikutnya. [Daftar kebutuhan seluruh aplikasi](FULL-APP-STATUS-2026-10-09.md) tetap terbuka, termasuk pajak lanjutan, payroll, pemulihan marketing/operasi dan publikasi sumber untuk pengguna nonteknis. Bukti serta keputusan profesional perusahaan, kelengkapan transaksi, UAT staf, cloud HTTPS dan API live tetap perlu dipenuhi. Rilis ini tidak mengirim atau menghasilkan SPT resmi DJP.
