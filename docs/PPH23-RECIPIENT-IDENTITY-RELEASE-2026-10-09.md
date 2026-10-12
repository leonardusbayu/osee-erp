# Rilis terbatas: identitas penerima dan bukti PPh 23

Status **READY_FOR_SCOPE** untuk TAX-02 pada source `e6a35cfd1f64e267286477898d28d53145766985baa2343d885b230bc3e6ffd3`. Aplikasi lokal telah diperbarui dan diperiksa di `http://127.0.0.1:8001`. Ini penerimaan kemampuan yang dijelaskan di bawah, bukan pernyataan seluruh ERP siap produksi atau hasil pajak perusahaan telah benar.

## Perubahan yang dapat dipakai

Finance atau Owner menyiapkan identitas PT sendiri atau pemasok dari dokumen, kemudian Owner mencatat laporan pemeriksa profesional yang diterima. Identitas, dokumen asli, keputusan, sumber hukum dan tanggal berlaku disimpan sebagai versi. Nomor kosong atau nomor yang baru diketik tetap belum terkonfirmasi; sistem tidak memilih faktor tarif hanya dari format nomor.

PPh 23 baru mengikat penerima, pemotong, objek, dasar pengenaan dan tanggal terutang yang dinyatakan dalam keputusan. Potongan pelanggan memakai identitas PT sebagai penerima; pemotongan pemasok memakai identitas pemasok serta identitas PT sebagai pemotong. Keputusan untuk NIK membedakan validasi, integrasi dan aktivasi. Nomor sensitif disamarkan di halaman ringkasan.

Dokumen potong pelanggan harus cocok dengan kedua pihak, objek, dasar, nominal, tanggal terutang, masa dan status bukti. File PDF yang disetujui tidak dapat diganti hanya dengan memperbarui metadata. Penjagaan yang sama digunakan sebelum pembayaran pemasok, penyetoran potongan, pelunasan neto pelanggan, kredit dari Allocation lama dan persiapan kredit tahunan. Pengulangan transaksi yang sudah selesai mengembalikan hasil lama tanpa membuat kas baru.

Data yang belum terkonfirmasi dapat dilengkapi sebagai versi pengganti dengan tanggal berlaku sebenarnya, selama tidak mengubah kewajiban disetujui atau transaksi yang sudah memakai keputusan lama. Draf identitas yang keliru juga dapat diganti tanpa menghapus riwayatnya. Perubahan substansi hukum atau fakta yang telah dipakai tetap memerlukan alur koreksi tersendiri.

Halaman memakai instruksi Bahasa Indonesia, nilai input tetap tersedia saat validasi gagal, tanggal tampil benar, dan rincian audit berada dalam bagian yang dapat dibuka. Kertas kerja pelanggan menampilkan bukti potong yang tercatat serta langkah pelunasan Finance; status SPT Tahunan tetap terpisah. Riwayat lama yang belum mengikat identitas tetap dapat dibaca, tetapi tidak mendapat izin transaksi baru secara otomatis.

## Bukti penerimaan

Semua pengujian akhir berikut memakai source yang sama, data sintetis, database dan dokumen terpisah, serta provider eksternal dimatikan.

| Pemeriksaan | Hasil |
|---|---|
| SQLite | 770 kasus dijalankan: 743 lulus, 27 khusus PostgreSQL dilewati |
| PostgreSQL 17 | 770/770 lulus, termasuk 27 kasus konkurensi database nyata |
| Pemeriksaan independen | 28/28 lulus untuk integritas, pemulihan, pembatasan dan riwayat |
| Chrome | 14 alur bisnis/60 keadaan halaman pada lebar 1440 dan 390; tidak ada error script atau overflow horizontal |
| Skema/kode/dependensi | Django check, konsistensi migrasi, Ruff error kritis, compile dan pip check lulus |
| Gate bukti | READY_FOR_SCOPE, sembilan temuan yang direproduksi diselesaikan |

Browser memverifikasi data tersimpan: invoice sintetis Rp500.000 ditutup oleh kas Rp490.000 dan kredit pajak Rp10.000; retry menghasilkan satu settlement, satu bank posting dan satu jurnal bank. Riwayat identitas yang belum terkonfirmasi tetap ada setelah versi pengganti disetujui. Angka ini oracle pengujian dari keputusan fixture yang disetujui, bukan penetapan pajak transaksi OSEE atau IIEF sebenarnya.

Plan terkunci, hasil, bukti berhash dan review berada di `.local/quality-20261009/recipient-final-1/`. Bukti browser akhir berada di `.local/recipient-tax-browser/run-labccnl6/`. Server browser sementara dan cluster PostgreSQL khusus QA dihentikan setelah pengujian. Kesalahan penyiapan fixture/perintah privat yang didiagnosis dan klaim review yang ditarik tidak dihitung sebagai temuan produk.

## Pembaruan lokal dan preservasi

Server OSEE dihentikan dengan pemeriksaan identitas proses workspace. Backup database dan dokumen diverifikasi sebelum migrasi `taxes.0010_recipient_identity`. Semua **937 catatan asli pada 117 tabel** tetap cocok per primary key dan kolom lama setelah migrasi dan penyiapan sumber. Profil pajak serta publikasi lama tidak berubah. Tidak ada identitas penerima nyata yang dibuat atau disetujui.

Empat sumber identitas baru disiapkan sebagai **belum dipublikasikan**: `pph23-identity-law`, `pmk81-identity-2024`, `per7-2025`, dan `peng6-2024`. Penyiapan ini tidak mengaktifkan perlakuan perusahaan atau mengganti sumber lama yang terpublikasi.

Backup: `.local/backups/before-recipient-identity-20261009T104450Z.zip`. Bukti preservasi: `.local/quality-20261009/recipient-company-preservation-20261009T104450Z.json`. Delapan pemeriksaan HTTP setelah restart lulus untuk health, readiness, login, penjagaan login halaman baru dan aset. Bukti: `.local/quality-20261009/recipient-preview-http-20261009.json`. Aplikasi lain pada port8000 dan layanan PostgreSQL sistem tetap hidup.

## Batas yang tetap terbuka

Dokumen dan keputusan pajak perusahaan yang sebenarnya belum diperiksa dalam QA ini. Aplikasi belum menyatakan laporan dikirim ke DJP, menyediakan XML Coretax resmi, atau membuktikan SPT perusahaan benar. Persetujuan profesional, kelengkapan sumber, UAT staf dan cloud HTTPS masih diperlukan.

[FIN-05](ADVANCE-WITHHOLDING-CREDIT-PLAN-2026-10-09.md) berikutnya menangani bukti potong setelah penerapan uang muka pelanggan. Supplier advance, pelunasan pemasok parsial/akrual/lintas masa, pemotongan final pelanggan, pembetulan resmi dan rekonsiliasi fiskal tahunan terperinci tetap pekerjaan terpisah. Publikasi sumber hukum global saat ini masih memakai perintah operator; UX-04 memerlukan antarmuka dan kewenangan yang dirancang dengan benar. Seluruh backlog tetap tercatat dalam [status aplikasi](FULL-APP-STATUS-2026-10-09.md).
