# Panduan Finance OSEE

Panduan ini untuk Owner/Direktur dan Finance. Marketing memakai workspace Marketing dan tidak memperoleh akses mutasi, dokumen pajak, atau data akun Finance.

## Mulai dari rekening dan e-statement

1. Owner membuka **Mutasi Bank → Tambah rekening bank**, lalu memasukkan identitas rekening yang benar. Ini belum menghubungkan API atau memberi izin transfer.
2. Jika memulai buku dari suatu tanggal, Owner menggunakan **Catat saldo awal** dengan rekening koran sebagai bukti. Saldo berlaku pada awal hari yang dipilih. Pembukaan ini hanya mencatat Bank dan modal awal; saldo piutang, utang, dan neraca perusahaan tetap perlu direkonsiliasi sebelum buku digunakan sebagai laporan lengkap.
3. Finance membuka **Impor mutasi**, memilih rekening, lalu mengunggah Excel `.xlsx`/`.xls` asli atau CSV. Maksimum 5 MB dan 5.000 transaksi per file. File HTML yang hanya diberi nama `.xls`, Excel terenkripsi, serta formula dalam `.xlsx` tidak didukung.
4. Cocokkan kolom tanggal, referensi, keterangan, dan nominal. Untuk rekening bank, **debit berarti uang keluar** dan **kredit berarti uang masuk**. Pilih format angka Indonesia (`1.234.567,89`) atau internasional (`1,234,567.89`) sesuai sumber; angka Excel asli dipertahankan.
5. Jika file memuat saldo awal atau baris total, atur rentang agar hanya transaksi yang dipilih. Tanggal harus lengkap dengan tahun dan sudah terjadi.
6. Buka pratinjau. Periksa rekening, jumlah transaksi, total masuk/keluar, perubahan saldo, dan beberapa transaksi terhadap file asli. Kesalahan harus diselesaikan sebelum konfirmasi. Pratinjau menampilkan paling banyak 100 transaksi; sumber lengkap tetap dapat diunduh.
7. Centang pemeriksaan dan **Konfirmasi impor**. Referensi yang sudah ada dengan isi sama dilewati; referensi sama dengan isi berbeda membatalkan seluruh konfirmasi. Mengunggah file yang sama pada rekening yang sama membuka sumber yang sudah tersimpan.

Jika bank tidak menyediakan nomor referensi unik, opsi identitas dari tanggal, keterangan, nominal, dan saldo berjalan tersedia. Gunakan cara identifikasi yang sama secara konsisten untuk rekening tersebut dan periksa transaksi yang terlihat kembar. Jangan mengganti cara identifikasi pada impor yang saling bertumpang tindih tanpa pemeriksaan.

File yang baru diunggah belum menambah mutasi. Mutasi yang sudah dikonfirmasi belum otomatis menjadi pendapatan atau beban. **Cocokkan dokumen** untuk menjelaskan setiap transaksi.

## Menyelesaikan mutasi

- **Pembayaran invoice:** pilih invoice yang sesuai dan jumlah yang dialokasikan. Penyelesaian tes mitra mensyaratkan pembayaran yang tercatat sampai tanggal layanan.
- **Uang masuk sebelum invoice:** buka **Uang muka & transaksi lain → Catat uang muka**. Pilih pelanggan/mitra dan bukti bank, lalu alokasikan ke invoice pelanggan yang sama ketika invoice tersedia.
- **Biaya bank:** buka tindakan biaya bank pada mutasi keluar. Sebagian mutasi dapat menjadi biaya dan sisanya tetap tersedia untuk pencocokan lain.
- **Transfer rekening perusahaan:** pilih satu mutasi keluar dan satu mutasi masuk pada rekening berbeda. Biaya transfer dicatat terpisah.
- **Setoran pemilik:** Owner memilih setoran modal pada mutasi masuk dengan bukti yang sesuai.
- **Pembatalan sebelum layanan:** Owner membatalkan seluruh pesanan. Penerimaan yang sudah ada menjadi utang refund. Finance mencocokkan transfer keluar pada **Refund pesanan dibatalkan**. Untuk uang muka yang belum digunakan, gunakan refund uang muka.
- **Biaya dibayar di muka:** tagihan untuk layanan masa depan dibukukan sebagai aset dibayar di muka. Setelah layanan diperoleh, buka tagihan dan **Akui beban layanan**, isi tanggal, jumlah, dan referensi bukti. Pengakuan sebagian biaya tersedia.
- **Tagihan dengan potongan pajak:** selesaikan review pajak terlebih dahulu, lalu gunakan **Cocokkan pembayaran neto** pada tagihan. Sistem mencatat utang potongan terpisah. Pilihan ini saat ini untuk pelunasan penuh pada masa potongan yang sama. Setoran utang potongan dicocokkan dari rincian kewajiban pajak.

Tindakan tersebut mencatat bukti transaksi yang sudah terjadi. Aplikasi tidak mengirim transfer bank. Pembatalan sebagian, refund setelah layanan, pembayaran pemasok sebelum tagihan, dan pengakuan pendapatan kursus per sesi memerlukan alur tambahan; jangan memasukkannya sebagai biaya bank atau memaksakan tanggal agar diterima.

## Pemeriksaan bulanan dan pajak

1. Lengkapi dokumen penjualan, bukti layanan, tagihan pemasok, dan e-statement seluruh rekening. Selesaikan mutasi yang belum dicocokkan dan biaya dibayar di muka yang sudah jatuh waktu pengakuannya.
2. Lengkapi fakta pendaftaran dan status pajak dari dokumen perusahaan. Omzet di bawah Rp4,8 miliar dan non-PKP tidak otomatis mengaktifkan tarif final untuk PT biasa.
3. Finance menyiapkan kewajiban atau review tagihan. Owner mencatat hasil pemeriksaan profesional yang kompeten beserta laporan PDF, identitas pemeriksa, referensi kualifikasi, tanggal, dan keputusan yang diperiksa. Status Owner sendiri tidak menggantikan kompetensi perpajakan.
4. Periksa nominal dengan mesin hitung dan dokumen. Setelah pembayaran/pelaporan benar-benar dilakukan, unggah bukti resmi dan catat pemeriksaan kecocokan identitas, masa, jenis, nominal, serta referensi resmi. Bukti bank bukan pengganti seluruh bukti DJP.
5. Buka laporan dan selesaikan penghalang penutupan. Bulan tertutup melindungi transaksi bulan itu dan semua tanggal sebelumnya yang memengaruhi saldonya. Penutupan bukan pernyataan bahwa data di luar aplikasi sudah lengkap.
6. Persiapan tahunan menghasilkan **kertas kerja fiskal berversi** untuk diperiksa. Ekspor paket tersebut tetap draf kerja; belum merupakan format SPT yang telah disertifikasi atau diterima Coretax. Lengkapi penyusutan, koreksi fiskal, kredit pajak, lampiran, dan proses resmi sesuai kondisi perusahaan.

Chat pajak membantu memahami sumber dan langkah pemeriksaan. Jawaban AI tidak mengubah tarif, membukukan transaksi, atau membuktikan penerimaan laporan. Rujukan yang belum dipublikasikan melalui pemeriksaan atau sudah tidak segar tidak dipakai sebagai panduan aktif. Kunci serta penerimaan layanan OpenRouter harus disiapkan sebelum mode eksternal digunakan.

## Akun tim

Owner dapat membuat tiga peran: Owner/Direktur, Finance, Marketing. SDM dan penggajian direncanakan di bawah Finance; modulnya belum diaktifkan.

Gunakan **Pengaturan → Kelola akses** untuk menonaktifkan anggota yang keluar. Riwayatnya tetap disimpan. Sistem melindungi akses Owner terakhir dan tidak mengizinkan Owner menonaktifkan dirinya sendiri.

Owner dapat mengatur sandi sementara anggota setelah memverifikasi identitas. Sesi lama dibatalkan dan anggota wajib mengganti sandi setelah masuk. MFA tetap berlaku. Akun yang dipakai beberapa perusahaan memerlukan prosedur operator karena sandinya bersifat global.

Buka **Keamanan akun** untuk memasang Authenticator dan menyimpan kode pemulihan di tempat privat. Kode pemulihan hanya ditampilkan sekali dan setiap kode hanya dapat digunakan sekali. Cloud mewajibkan MFA. Jika seluruh akses pemulihan hilang, gunakan prosedur administrator server di [panduan deployment](../deploy/README.md), dengan verifikasi identitas dan audit.

## Data perusahaan yang belum tersedia

Rekap 2026 yang telah diarsipkan tetap merupakan sumber observasi, bukan pengganti mutasi, invoice, buku besar, atau omzet pajak. Impor e-statement asli dan penyelesaian selisih sumber masih harus dilakukan ketika dokumennya tersedia. API BNI masih dalam proses review; belum ada sinkronisasi otomatis yang diaktifkan.
