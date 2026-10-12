# Memperbaiki pelanggan dan produk dari invoice yang keliru

MKT-02 menangani satu alur pemulihan: Finance mengonfirmasi invoice pada lead yang pelanggan/produknya masih kosong, lalu menyadari invoice yang dipilih salah. Pada source e32, dua reproduksi publik Owner/Finance membuktikan pembuatan lead, penerbitan dua invoice dan inferensi pertama berhasil; invoice pengganti berbeda pelanggan/produk tidak ditawarkan oleh koreksi atribusi lama, dan form pemulihan identitas mengembalikan404. GET tidak mengubah lead, audit asal atau Finance. Probe asli dan oracle tetap disimpan untuk replay, bukan diubah agar cocok dengan implementasi.

## Hasil yang perlu dilihat staf

Dari detail lead, Owner/Finance membuka **Perbaiki pelanggan atau produk dari invoice**. Halaman menjelaskan dari mana pelanggan dan produk terisi, menampilkan nilai sekarang, lalu menawarkan **Pilih invoice yang benar** atau **Belum tahu invoice yang benar**. Pelanggan/produk yang sudah diketahui sejak pendaftaran atau berasal dari kampanye tetap dipertahankan.

Staf melihat perbandingan sebelum/sesudah, memasukkan alasan dan referensi bukti, kemudian mengonfirmasi. Bila sumber berubah, masukan tetap terlihat dan staf harus memeriksa versi terbaru sebelum mengonfirmasi lagi. Hasil tersimpan menunjukkan perubahan, nilai yang kembali belum diketahui, sumber awal, pencatat/waktu serta riwayat. Jalur ini tetap tersedia setelah hubungan invoice keliru sebelumnya dilepas.

Marketing membaca riwayat pada lead yang dapat diakses, tetapi tidak mendapat kewenangan mengganti identitas invoice. Auditor hanya membaca. Penulis mengikuti Owner/Finance aktif organisasi yang sama sebagaimana koreksi atribusi operasi saat ini; peran lama tidak diperluas diam-diam.

## Sumber identitas dan riwayat

Jangan menyimpulkan sebuah nilai berasal dari invoice hanya karena nilainya sama. Asal dicatat per kolom: pelanggan manual, produk kampanye, atau nilai yang benar-benar kosong sebelum konfirmasi invoice. Konfirmasi hubungan Order tidak mengisi identitas lead dan tidak menjadi bukti asal inferensi.

Hubungan invoice baru harus mencatat asal bertipe dan riwayat immutable dalam transaksi yang sama. Koreksi mengikuti rantai itu; pelepasan/penautan ulang, pergantian PIC atau kontak tidak menghapus asal pertama. Setiap keputusan mengikat organisasi, lead, invoice sumber/tujuan, nilai sebelum/sesudah, kolom yang boleh diperbaiki, actor/waktu, alasan/bukti, versi, fingerprint dan identitas permintaan. Riwayat lama tidak ditulis ulang.

Audit lama adalah model biasa yang belum menjamin keutuhan historis. Pointer atau salinan digest tidak membuktikan bahwa isi sebelum adopsi belum pernah diubah. Riwayat lama harus lolos validasi ketat atas tipe, scope, konfirmasi, null sebelum inferensi, nilai sesudahnya serta urutan kejadian. Adopsi untuk pemulihan harus berupa keputusan review sumber yang terlihat dan terdokumentasi; jangan otomatis menyebutnya asal immutable. Riwayat hilang, bertentangan atau ambigu menampilkan tugas **Periksa sumber pelanggan/produk** dan memblokir perubahan yang tidak dapat dibuktikan. GET tidak boleh membuat adopsi, keputusan atau marker baru.

## Batas perubahan

Penggantian hanya boleh menyentuh kolom yang terbukti terisi oleh inferensi invoice keliru. Kolom manual/kampanye tetap terkunci dan invoice tujuan harus sesuai dengannya. **Belum tahu invoice yang benar** mengembalikan hanya kolom inferensi ke nilai sebelum inferensi, biasanya kosong, dan melepas hubungan invoice keliru; tidak membuat pelanggan/produk palsu.

Invoice tujuan harus berada dalam organisasi yang sama, sudah issued/delivered, bertanggal sesuai lead, serta tidak bertentangan dengan lead/order lain. Invoice asal yang kemudian dibatalkan tetap menjadi bukti historis; itu tidak menjadikannya invoice tujuan yang sah.

Order yang masih terkait bukan berarti sudah terlepas hanya karena hubungan invoice langsung dikosongkan. Bila atribusi Order keliru, halaman menunjukkan Order dan tindakan koreksi atribusi yang sudah tersedia. Perbaikannya dilakukan eksplisit dahulu; pemulihan identitas tidak boleh otomatis mengganti pembeli, produk, peserta atau invoice Order.

Invoice, harga, bukti/pelunasan pajak, jurnal, kas, mutasi bank, allocation, fulfillment, kredit/refund, Order, baris dan peserta harus tetap sama. Pembayaran invoice A tetap pembayaran A. Setelah lead menunjuk B, status pembayaran mengikuti pelunasan nyata B; uang A tidak dipindahkan atau dihitung sebagai uang B.

## Transaksi, pembaca dan kesegaran

Kunci organisasi diperoleh sebelum lead dan dependensi sumber/tujuan. Validasi actor, asal, target, Order, versi dan fingerprint dilakukan ulang di dalam transaksi. Permintaan ulang yang persis sama mengembalikan keputusan tersimpan sebelum pemeriksaan kesegaran; isi berbeda dengan request yang sama ditolak. Pergantian bersaing, reset lawan penggantian dan koreksi Order bersamaan harus menghasilkan satu keputusan konsisten atau penolakan stale, tanpa setengah perubahan.

Gunakan penjaga mutasi model yang sempit dan berakhir setelah operasi; jangan melemahkan perlindungan identitas lead maupun dokumen terbit secara umum. Log, audit asli, kontak, campaign/channel, PIC, nama/reference/tanggal penerimaan tidak diganti.

KPI dan atribusi membaca satu pemetaan bersama. Lead version serta lineage identitas/atribusi mengubah fingerprint lokal walaupun nominal pengganti sama, tidak ada KPI atau Order, atau reset/taut ulang mengembalikan nilai yang sama. Evaluasi dan saran lama tetap terbaca namun tidak dapat dikonfirmasi sebagai evaluasi sumber terbaru. Konfirmasi stale mempertahankan masukan dan tidak membuat pekerjaan baru. ID, alasan, bukti dan fingerprint tetap lokal; whitelist agregat OpenRouter tidak diperluas.

## Penerimaan

1. Replay kedua probe publik asli setelah inferensi normal; form tersedia tanpa GET write.
2. Penggantian ke B dan reset sementara tersimpan dengan perbandingan/riwayat; dokumen dan transaksi Finance/Operations tetap identik.
3. Asal campuran, produk kampanye, manual yang kebetulan sama dengan invoice, serta linkage Order tidak memberi izin mengganti kolom yang dilindungi.
4. Review/adopsi riwayat lama jelas; tipe/string/null/actor/scope/order ambigu atau audit yang berubah ditolak atomik.
5. Asal pertama bertahan setelah pelepasan invoice, kontak/PIC berubah, penggantian berulang dan reset/taut ulang; invoice asal dibatalkan tidak menghapus sejarah.
6. Bukti sumber/target berubah, invoice tujuan dibatalkan atau atribusi Order baru muncul setelah form dibuka memblokir penyimpanan stale dengan masukan tetap terlihat.
7. Retry, konflik payload, CSRF, actor tidak aktif/dicabut serta cross-organization ditegakkan server. Race PostgreSQL nyata memeriksa keputusan dan seluruh outcome tersimpan.
8. UI desktop/ponsel membuktikan inferensi → koreksi → baca ulang → riwayat, reset yang tetap dapat dipulihkan, tindakan dependensi Order, serta error/stale. Browser bukan bukti UAT pertama kali staf nyata.
9. Seluruh pemeriksaan akhir terikat source/test yang sama, gate scoped dan pemeriksaan preservasi perusahaan sebelum aplikasi dihidupkan kembali. Data asli bukan fixture; provider dimatikan.

Kontrak ini telah diimplementasikan dan diterima dalam cakupan MKT-02 pada source d7. Lihat [hasil pengujian dan preservasi perusahaan](MARKETING-INVOICE-IDENTITY-RECOVERY-RELEASE-2026-10-10.md). Backlog lengkap tetap berada pada [status aplikasi](FULL-APP-STATUS-2026-10-09.md).
