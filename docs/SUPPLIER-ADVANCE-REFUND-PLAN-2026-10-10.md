# Uang muka pemasok, penggunaan pada tagihan dan pengembalian

FIN03 telah diimplementasikan, diuji dan dipasang lokal pada source `34f0d185ddde260b7c9651e9bd1a35e716a1f78e8cbf48e483b0193cf0792923`. Kontrak di bawah menjadi acuan perilaku; bukti penerimaan aktual ada pada [laporan FIN03](SUPPLIER-ADVANCE-REFUND-RELEASE-2026-10-10.md) dan [backlog aplikasi](FULL-APP-STATUS-2026-10-09.md). Penerimaan terbatas mencakup uang pokok/pemakaian/refund/pembalikan penuh serta tinjauan tanpa pemotongan. Pemotongan positif dan koreksi kas/parsial tetap terbuka; seluruh ERP belum dinyatakan siap.

## Alur staf

1. **Catat uang muka pemasok:** pilih pemasok dan uang keluar yang sudah diimpor dari bank. Tunjukkan jumlah, tanggal bank dan referensi pembelian/deposit. Periksa sumber, lalu konfirmasi. Tidak melakukan transfer baru.
2. **Gunakan untuk tagihan:** pilih tagihan yang telah disahkan milik pemasok yang sama. Nominal tidak melebihi sisa uang muka atau sisa tagihan. Tunjukkan saldo sebelum/sesudah. Tidak membuat uang keluar kedua.
3. **Cocokkan pengembalian:** pilih penerimaan bank nyata untuk sisa uang muka yang belum dipakai. Nominal tidak melebihi sisa uang muka atau sisa penerimaan bank. Pengembalian bukan penjualan atau pembatalan biaya tagihan.
4. **Perbaiki penerapan yang keliru:** Owner membalik seluruh penerapan dengan alasan/bukti pada tanggal aktual yang masih terbuka. Catatan/jurnal lama tetap ada. Koreksi hubungan anggaran dilakukan secara eksplisit terlebih dahulu bila diperlukan.

Ringkasan menampilkan uang yang dibayar, dipakai pada tagihan, dikembalikan, dan masih tersisa. Riwayat memperlihatkan sumber dan tanggal sebenarnya. Detail teknis akuntansi tidak menjadi pertanyaan utama staf. Tugas tinjauan pajak tetap terlihat meskipun sisa uang sudah nol.

Owner dan Finance dapat mencatat/menggunakan/mencocokkan. Pembalikan penerapan memerlukan Owner. Peran pembaca Finance yang sudah ada tetap hanya membaca; Marketing tidak mendapat akses. Mutasi bank, pemasok, tagihan, akun, dokumen dan actor diperiksa dalam organisasi yang sama setiap penyimpanan. GET tidak mengubah catatan.

## Pembukuan dan tanggal

| Kejadian | Pembukuan | Tanggal |
|---|---|---|
| Uang keluar RpA | Aset uang muka pemasok bertambah A; Bank berkurang A | Tanggal asli bank |
| Uang muka dipakai RpX | Utang usaha berkurang X; aset uang muka berkurang X | Tanggal penerapan, paling awal tanggal uang muka dan tagihan |
| Uang kembali RpR | Bank bertambah R; aset uang muka berkurang R | Tanggal asli bank, tidak mendahului uang muka |
| Pembalikan penerapan RpX | Aset uang muka dan utang usaha bertambah X | Tanggal aktual terbuka, tidak mendahului penerapan |

Tagihan tetap menjadi sumber pengakuan biaya/PREPAID. Uang muka tidak menciptakan biaya, pendapatan, PPN masukan atau potongan pajak perkiraan. Kas memakai BankPosting yang sama dengan alokasi bank lama, sehingga satu mutasi tidak mempunyai kapasitas terpisah untuk fitur baru.

Pada **setiap tanggal**, aset harus sama dengan uang muka dibayar dikurangi penggunaan dan pengembalian, ditambah pembalikan. Utang per tagihan harus cocok dengan pengesahan dikurangi settlement yang berlaku pada tanggal tersebut. Pemeriksa menguji seluruh tanggal kejadian, bukan hanya saldo sekarang. Contoh yang harus ditolak: penerapan100 pada10 Januari dibalik20 Januari, lalu pembayaran100 bertanggal15 Januari digunakan untuk tagihan yang sudah lunas pada15 Januari. Saldo akhir saja tidak membuktikan laporan15 Januari benar. Tanggal bank tidak diganti untuk menghindari pemeriksaan.

Pembalikan memulihkan jumlah asli dan tidak dibatasi sisa uang muka/utang saat ini. Pembayaran dan pengembalian nyata yang terjadi setelah penerapan tetap ada. Namun seluruh saldo historis, keutuhan sumber dan hubungan anggaran harus valid. Kas yang salah pemasok, refund keliru, penghapusan alokasi bank dan pembalikan sebagian membutuhkan kontrak koreksi kas tersendiri; jangan menghapus catatan atau membuat klaim berlawanan pada mutasi asli.

## Pajak dan keputusan profesional

Kas dicatat berdasarkan jumlah bank nyata; jumlah kontrak bruto dan potongan tidak ditebak dari kas bersih. Status awal membutuhkan tinjauan. Keputusan atas tagihan belakangan tidak otomatis memutuskan perlakuan pembayaran uang muka sebelumnya.

Penjelasan [DJP tentang PPh23](https://pajak.go.id/index.php/id/pph-pasal-2326) menghubungkan saat pemotongan dengan pembayaran, ketersediaan pembayaran atau jatuh tempo. **Implikasi desain:** ketiadaan invoice tidak cukup untuk otomatis menganggap potongan nol. Halaman edukasi tersebut bukan dasar untuk menyalin tenggat atau mekanisme pelaporan lama ke aplikasi; sumber pelaporan mutakhir tetap mempunyai proses tinjauan sendiri.

Pencatatan aset boleh berlangsung dengan tugas pajak terbuka. Keputusan profesional harus terikat sumber uang muka, pemasok, bank/jurnal, bukti PDF asli dan isi keputusan yang tepat. Perubahan sumber/bukti membuat keputusan tidak lagi berlaku. Keputusan bebas potongan hanya dapat direkam dari laporan profesional yang benar-benar diterima, bukan checkbox staf atau kesimpulan AI. Perlakuan potongan positif/gross-up yang belum didukung tetap terlihat sebagai pekerjaan yang belum selesai; aplikasi tidak mengarang jurnal pajaknya.

Penerapan awal memakai tagihan dengan keputusan potongan nol yang sudah melalui alur profesional saat ini. Tinjauan uang muka adalah keputusan tersendiri. Pending/stale/tidak didukung harus menghalangi tutup buku terkait dan persetujuan/paket fiskal yang bergantung sumber tersebut; tampilan jurnal dan draf fakta komersial tetap menampilkan kas nyata. Daftar endpoint, aturan kesegaran dan alur pelepasan blocker ditentukan dari workflow pajak yang sudah ada sebelum integrasi.

## Anggaran dan kontrol sumber

Penerapan mengurangi utang tanpa menambah kas atau biaya. Alokasi kas lama ke tagihan/anggaran menyimpan **tanggal kas asli** dan **tanggal hubungan tersedia**. Alokasi hanya terlihat pada cutoff setelah hubungan memang ada; matching tidak dibackdate sebelum penerapan/hubungan. Beberapa tagihan boleh memakai satu uang muka, tetapi total kas yang diatribusikan tidak melebihi penggunaan efektif maupun uang muka asli. Sisa belum dipakai dan pengembaliannya tidak dimasukkan ke anggaran sembarang.

Hubungan anggaran menghitung kas sumber sekali dan penerapan nol kas. Manual-payment matching menunjuk sumber bertipe yang tepat, dengan bukti bank asli. Sumber hilang, lintas organisasi atau berubah berarti belum dapat dinilai; tidak disembunyikan menjadi angka nol. Sinkronisasi Finance/anggaran menjadi bagian transaksi yang sama.

Seluruh tindakan memakai lock organisasi lebih dahulu, actor aktif, sumber bertipe immutable, fingerprint, alasan/bukti dan request key. Retry dengan payload persis mengembalikan keputusan lama tanpa membuat jurnal/riwayat baru, termasuk setelah alokasi habis, pembalikan atau tutup periode. Payload berbeda ditolak. Keputusan dibalik tidak diterapkan kembali lewat retry lama. Kesalahan stale mempertahankan isian; periksa ulang menghapus konfirmasi dan memberi key baru.

## Pembuktian yang diperlukan

- Oracle publik asli pada d7: dua tes, tiga assertion gagal (modul pencatatan belum ada; form Owner/Finance404); database sintetis dihancurkan normal dan source sebelum/sesudah tetap d7. Probe tetap utuh untuk replay setelah implementasi.
- Rantai bank -1.000 → tagihan600 → gunakan400 dan200 → bank kembali150 dan250: aset akhir0, utang0, kas neto-600, biaya/PREPAID600. Periksa setiap saldo antara, bukan hanya hasil akhir.
- Reversal, pembayaran biasa setelah uang muka, biaya layanan masa depan, pending pajak setelah saldo0, periode tertutup, sumber rusak, rollback, retry, scope/peran dan bukti tidak berubah.
- PostgreSQL nyata untuk persaingan satu mutasi, penggunaan/pengembalian, penggunaan/pembayaran tagihan, retry, reversal dan tutup periode; cleanup normal tanpa force-drop.
- Integrasi tutup buku, laporan bertanggal, bank, anggaran, paket pajak serta regresi pelanggan/pajak lama. Browser desktop/ponsel memperlihatkan sumber → periksa → simpan → baca ulang → riwayat, stale dan akses pembaca.
- Pembaruan perusahaan hanya setelah source yang sama lulus pemeriksaan, dengan backup/preservasi asli serta migrasi terbatas. Cloud, provider live, data pajak perusahaan dan UAT staf bukan fixture pengembangan.

Kesalahan perintah runner awal (`.venv-unavailable`) gagal sebelum membuka interpreter tes; log kosongnya dipertahankan sebagai kesalahan alat, bukan kegagalan produk. Oracle sebenarnya dijalankan ulang dengan interpreter `.venv` absolut, label `supplier-advance-original-public-20261010-2`, probe SHA256 `24a77d6731ad28ff7d8c9ea8f9cbe1e36750dc1046985e5ab0e432ec2c14a44f`.
