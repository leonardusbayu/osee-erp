# Koreksi jumlah piutang dan utang lama

Status rencana: implementasi dan pengujian di checkout terpisah. Source perusahaan yang diterima sebelumnya adalah `bf4a8d16ce3ecf07fb406786308a3735f9ca2feb7bc8e0d9b7902d26a438f9c7`. Dokumen ini tidak menyatakan perubahan sudah terpasang atau seluruh ERP siap.

## Tujuan dan alur staf

Owner membuka rincian piutang atau utang lama, memilih **Perbaiki jumlah**, lalu **Tambah** atau **Kurangi**. Formulir meminta tanggal berlaku, jumlah perubahan yang positif, alasan, dan bukti. Langkah berikutnya memperlihatkan saldo sebelum/sesudah. Setelah konfirmasi, Finance dapat melihat saldo dan riwayatnya. Tidak diperlukan pemilihan akun jurnal atau mutasi bank untuk koreksi jumlah.

Koreksi mencatat perubahan terhadap pokok lama. Dokumen, pihak, tanggal awal bersama, jumlah asli dan bukti asal tetap tersimpan. Pembatalan pencocokan bank serta pengembalian uang nyata merupakan alur berikutnya dengan kontrol kas tersendiri.

## Batas keuangan

| Perubahan | Debit | Kredit |
|---|---|---|
| Tambah piutang lama | Piutang | Ekuitas saldo awal |
| Kurangi piutang lama | Ekuitas saldo awal | Piutang |
| Tambah utang lama | Ekuitas saldo awal | Utang |
| Kurangi utang lama | Utang | Ekuitas saldo awal |

Koreksi tidak menghasilkan invoice, tagihan pemasok, kas, penjualan, biaya, penyerahan layanan atau keputusan pajak baru. Jumlah efektif dan sisa tidak boleh negatif pada setiap tanggal kejadian, termasuk pembayaran yang sudah tercatat setelah tanggal koreksi. Pembayaran bertanggal lebih awal tidak boleh memakai tambahan pokok yang baru berlaku kemudian.

Posisi laporan memakai tanggal yang dipilih. Koreksi valid di masa berikutnya tidak mengubah saldo pada tanggal sebelum koreksi. Bukti rusak, hubungan sumber yang hilang atau jurnal yang tidak cocok menghasilkan posisi belum dapat dinilai, bukan saldo nol.

## Kewenangan dan perlindungan

Hanya Owner aktif pada perusahaan yang sama dapat menyimpan koreksi. Finance membaca hasil dan tetap memakai alur pembayaran yang tersedia. Marketing tidak mendapatkan akses ke rincian keuangan ini. Akses diperiksa sebelum percobaan ulang permintaan yang sama.

Penyimpanan memakai transaksi dan kunci perusahaan, bukti sumber yang diperiksa ulang, identitas permintaan sekali pakai, serta konfirmasi eksplisit. Percobaan ulang yang identik dan masih utuh tidak menghasilkan jurnal atau riwayat tambahan. Ketika sumber berubah setelah pratinjau, isian tetap tersimpan tetapi staf harus memeriksa ulang dan mengonfirmasi ulang.

Tanggal tertutup tidak dapat dikoreksi. Koreksi yang akan mengubah sumber laporan bulanan yang disetujui atau laporan tahunan yang disetujui/dilaporkan ditahan. Jika alur revisi resmi mengizinkan, persetujuan lama harus dilepas secara eksplisit saat sumbernya masih sesuai, sebelum koreksi keuangan. Sistem tidak melepas persetujuan atau menarik kewajiban secara otomatis.

Koreksi tanpa kas merupakan konteks pokok dan ketergantungan laporan. Koreksi ini tidak dianggap omzet, pemotongan atau kredit pajak. Bukti dan pemeriksaan fiskal atas pembayaran asli tetap terpisah dan tidak ditulis ulang.

## Batas pengujian yang dipakai

Pengujian memakai layanan publik pencatatan/koreksi/posisi, laporan saldo, alur laporan pajak resmi, dan endpoint HTTP yang dipakai staf. Oracle pertama mempertahankan piutang asli Rp300, menambah Rp100 pada tanggal kemudian, dan mengharapkan saldo historis Rp300/Rp400 serta ekuitas -Rp400 tanpa pendapatan atau biaya. RED asli setelah fixture diperbaiki disimpan; assertion perilaku tidak diubah untuk membuatnya lulus.

Verifikasi lanjut mencakup utang, pengurangan/menjadi nol, pembayaran sebagian, batas waktu, kewenangan, lintas perusahaan, permintaan kedaluwarsa, percobaan ulang, sumber rusak, persetujuan fiskal, transaksi bersamaan di PostgreSQL, serta layar desktop/ponsel dan alur tanpa JavaScript. Data perusahaan tidak dipakai sebagai fixture.

Pemasangan hanya dilakukan setelah hasil aktual pada source yang sama diterima. Backup source/database/media, migrasi terbatas, perbandingan seluruh baris asli dan unggahan, serta restart melalui launcher resmi harus diverifikasi. Dokumen rilis tersendiri akan mencatat hasil dan batas yang benar-benar diuji.
