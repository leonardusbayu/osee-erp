# Pembatalan salah pencocokan kas saldo lama — rilis 10 Oktober 2026

Status: **INSTALLED_AND_VERIFIED_FOR_SCOPE**. Source `caf0a3e92f20045eb6c3e4ffe593c6226f9dddc432344a805df42b3c2de25da2` dipasang lokal pada `20261010T000335Z`. Seluruh ERP belum dinyatakan siap: `all_erp_ready: false`; UAT staf dan deployment cloud belum selesai.

## Yang tersedia

Owner dapat membatalkan seluruh pencocokan piutang/utang lama yang keliru, termasuk pembayaran yang terlihat lunas. Jumlah dan tanggal bank asli tetap. Alur: buka dokumen saldo lama → pembayaran asli → **Batalkan pencocokan** → isi alasan/bukti → **Periksa sumber** → konfirmasi dan simpan. Jika sumber berubah, isian tetap tersimpan dan pemeriksaan serta konfirmasi baru diperlukan. Alur ponsel berjalan tanpa JavaScript.

Pembatalan menyimpan keputusan dan jurnal pembalik; bukti, jurnal, mutasi bank, dan pembayaran asli tetap ada. Mutasi yang dilepas dapat dicocokkan ulang dengan kapasitas bersama yang divalidasi. Tindakan ini tidak mengirim atau mengembalikan uang nyata. Finance membaca riwayat; pembatalan memerlukan Owner. Marketing tidak mendapat akses ke data Finance.

Laporan pajak tetap memerlukan bukti profesional dan klasifikasi eksplisit. Kas dibatalkan/pencocokan baru memiliki lineage agar kas fisik tidak dialokasikan dua kali. Review diterima, kewajiban berlanjut, laporan tahunan terlindungi, atau buku tertutup tetap menahan tindakan. Audit asli menjaga proteksi jika hubungan laporan profesional rusak. Bukti rusak/orphan/foreign ditampilkan sebagai belum dapat diperiksa; halaman bank dan ringkasan menahan angka/tindakan, dan riwayat tidak disebut kosong.

## Bukti penerimaan

SQLite seluruh aplikasi: **1.208 total, 1.142 PASS, 66 SKIP**. Lane gabungan: **275 total, 257 PASS, 18 SKIP** di SQLite; **275 PASS, 0 SKIP** di PostgreSQL, termasuk competing rematches. Teardown normal; PostgreSQL QA dihentikan normal dan listener asing tetap utuh. Oracle pokok sebelumnya dan semua regresi asli tetap sama. Django check, migration check, Ruff fatal checks, kompilasi 18 file Python berubah, dan pip check lolos.

Browser sintetis: **9 konteks, 41 tampilan, 28 screenshot, 9 native financial readbacks**; lima konteks ponsel tanpa JavaScript. Sumber stale, akses ditolak, bukti rusak, tanggal, jumlah, kapasitas, dan riwayat diperiksa. Tidak ada script error, request eksternal, atau overflow horizontal. Kegagalan alat uji raw/public comparison dipertahankan beserta diagnosis readonly; perbandingan diperbaiki tanpa mengubah kode aplikasi atau oracle uang.

Pemasangan: backup DB/media dan source terverifikasi sebelum copy; 23 file kode, satu migration0016, register pembatalan kosong, lima metadata framework baru. Seluruh nilai/kolom/baris historis, schema lama, sequence bisnis, timestamp migration lama dan upload terjaga. Tidak ada seed transaksi, autentikasi perusahaan oleh QA, atau panggilan provider. Tiga belas rute/asset diperiksa setelah restart normal. Bukti rinci privat tersedia pada `.local/quality-20261010-recovery`; jangan unggah DB/backup/credential ke GitHub.

## Pekerjaan berikutnya

Refund uang nyata beserta pemeriksaan pajaknya; pelepasan review profesional, pembatalan sebagian/amendemen periode; recovery paket bulanan yang sudah stale karena kas kemudian; kas saldo lama dengan pemotongan; saldo lama selain AR/AP, aset/liabilitas pajak lama; kelengkapan sumber perusahaan, UAT staf, BNI API resmi, dan cloud/domain/HTTPS. Semua tetap bagian tujuan aktif. [Rencana awal](OPENING-CASH-RECOVERY-PLAN-2026-10-10.md) disimpan sebagai riwayat, bukan bukti fitur refund sudah tersedia.
