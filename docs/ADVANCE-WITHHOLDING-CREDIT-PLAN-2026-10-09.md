# Bukti potong setelah penerapan uang muka pelanggan

Status: FIN-05 diterima dan diterapkan pada aplikasi lokal setelah pengujian final source yang sama, backup serta preservasi seluruh catatan asli. Lihat [bukti rilis dan batas cakupan](ADVANCE-WITHHOLDING-CREDIT-RELEASE-2026-10-09.md). Celah awal direproduksi melalui layanan publik dan halaman Finance; alur sekarang menerima `AdvanceApplication` terposting selain `Allocation` lama. Kontrak ini tidak mengubah tanggal atau perlakuan pajak perusahaan dan bukan penetapan bahwa setiap uang muka merupakan objek PPh23.

## Hasil yang dibutuhkan

Contoh sintetis: invoice Rp500.000 telah menerima penerapan uang muka Rp490.000. Setelah bukti potong Rp10.000 dicocokkan terhadap kewajiban PPh23 yang disetujui, Finance memilih penerapan uang muka yang sudah tercatat. Sistem menambahkan kredit pajak Rp10.000 untuk menutup sisa piutang. Uang bank tetap Rp490.000; jurnal uang muka dan penerapannya tidak berubah. Pengulangan permintaan mengembalikan hasil yang sama.

## Kontrak data dan transaksi

- Tambahkan `CustomerTaxSettlement.source_advance_application` dengan `PROTECT`. Migrasi Finance berikutnya harus mengganti constraint sumber menjadi tepat satu bukti: bank posting dengan kas positif, Allocation lama dengan kas nol, atau AdvanceApplication lama dengan kas nol. Riwayat lama dan constraint satu penggunaan bukti potong tetap berlaku.
- Perluas layanan kredit terlambat agar menerima tepat satu `source_allocation` atau `source_advance_application`. Jangan membuat penerimaan bank, Allocation, atau penerapan uang muka baru. Jurnal tambahan hanya debit piutang pajak dan kredit piutang pelanggan sebesar potongan yang disetujui.
- Bukti berasal dari organisasi, pelanggan dan invoice yang tepat. Periksa jurnal uang muka `BANK / CUSTOMER_ADVANCE` serta jurnal penerapan `CUSTOMER_ADVANCE / AR`, jumlah, tanggal dan source key aslinya. Bukti tidak boleh hilang, berubah, dibalik atau dialokasikan ulang.
- Tetap gunakan penjagaan bersama TAX-02: identitas berlaku, objek, tanggal terutang yang dinyatakan profesional, laporan terikat, dan bukti potong yang cocok terhadap kedua pihak. Nomor atau harga terbaru dari master tidak mengganti snapshot invoice.
- Tanggal bank asli dapat mendahului tanggal invoice. Untuk dasar `paid`, tanggal bank harus sama dengan tanggal terutang yang disetujui; untuk `available`/`due`, tidak boleh mendahuluinya. Tanggal jurnal kredit baru harus memenuhi urutan bukti serta periode yang terbuka. Tidak ada penentuan tanggal pajak, prorata atau gross-up otomatis.
- Invoice harus aktif dan tidak mempunyai kredit layanan yang bertentangan. Sisa piutang, penggunaan tunggal bukti potong, referensi permintaan dan konkurensi tetap diperiksa. Uang muka yang telah diterapkan tidak ditambah atau dikurangi oleh pencatatan kredit ini.

## Perubahan konsumen

`taxes/monthly_sources.py` perlu mengenali jenis bukti ketiga beserta tanggal, identitas dan fingerprint sumbernya. `marketing/kpi.py` serta `operations/analytics.py` perlu membawa provenance penerapan uang muka; kredit dengan kas nol tidak boleh menjadi penerimaan kas baru. Pertahankan cutoff historis: jurnal kredit yang dicatat kemudian tidak mengubah kas pada bulan sebelumnya.

`webapp/operations_views.py` dan halaman kredit terlambat perlu menyediakan pilihan penerapan uang muka dengan invoice, tanggal dan jumlah yang mudah dibaca. Tombol pada kertas kerja menunjukkan pilihan yang sesuai dengan pembayaran yang tersedia. Finance memilih bukti yang telah tercatat, memeriksa ringkasan, lalu menyimpan sekali. Riwayat membedakan kas asli, penerapan uang muka, dan kredit pajak.

## Bukti penerimaan

Uji publik dan browser harus menjalankan Rp490.000 uang muka sebelum invoice → penerapan Rp490.000 → bukti Rp10.000 → kredit → sisa piutang nol. Bandingkan jurnal asli, saldo bank dan sisa uang muka sebelum/sesudah; retry menghasilkan satu settlement dan satu jurnal kredit.

Kasus penolakan mencakup perusahaan/pelanggan/invoice keliru, dua jenis bukti sekaligus, jurnal sumber rusak atau hilang, periode baru ditutup, tanggal mundur, invoice dibatalkan, kredit layanan, bukti dipakai lagi, dan retry dengan isi berbeda. Periode sumber lama yang sudah ditutup tidak perlu dibuka ulang untuk mencatat bukti baru pada periode terbuka.

PostgreSQL perlu menguji dua retry bersamaan, perebutan bukti yang sama antara alur uang muka/Allocation/kas neto, penerapan uang muka yang bersamaan dengan kredit, serta kredit yang bersamaan dengan pembatalan/kredit layanan. Gunakan urutan kunci organisasi dan sumber yang konsisten; jangan memakai mutex proses sebagai pengganti transaksi database.

Pemotongan final pelanggan, uang muka pemasok, pelunasan pemasok parsial/lintas masa dan pembetulan dokumen/SPT tetap merupakan kontrak terpisah. Bukti, kebijakan waktu terutang dan persetujuan nyata tetap perlu disediakan perusahaan; QA menggunakan data sintetis.
