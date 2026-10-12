# FIN04: saldo awal piutang dan utang

Implementasi terbatas sesuai kontrak ini telah diterima dan terpasang lokal: `READY_FOR_SCOPE`, `company_installation: installed_local`, `all_erp_ready: false`. Source freeze 4 persis `bf4a8d16ce3ecf07fb406786308a3735f9ca2feb7bc8e0d9b7902d26a438f9c7` pada worktree pengembangan dan PRIMARY; identitas ini menjadi guard sinkronisasi. Pengujian targeted SQLite/PostgreSQL, regresi SQLite lengkap, quality, replay probe asli dan perjalanan browser telah lulus pada source tersebut. Receipt penerimaan mencatat `accepted_for_scope` untuk pokok piutang/utang lama, kas nyata dan ketergantungan fiskal terpisah. Bukti dan batas hasil dicatat dalam `OPENING-AR-AP-RELEASE-2026-10-10.md`.

Aplikasi perusahaan di PRIMARY `D:\ERP osee` memakai FIN04 pada port `8001`. Pemasangan menjalankan tepat dua migrasi, mempertahankan seluruh baseline aktual 130 tabel/1.010 baris dan unggahan, serta menambah nol baris bisnis; tiga register bisnis baru tetap kosong. Backup, preservasi dan sebelas pemeriksaan runtime anonim telah lulus sesudah restart normal. Dokumen ini adalah kontrak kerja; penerimaan alur terbatas tidak menjadi pernyataan seluruh ERP siap dipakai. UAT staf dan kemampuan yang disebut pada bagian batas tetap terbuka.

## Alur staf dan catatan asli

Owner mencatat sisa piutang pelanggan/mitra atau utang pemasok yang benar-benar ada pada tanggal awal pembukuan. Tanggal dokumen asli, nomor, pihak, sisa nominal, alasan dan bukti rekonsiliasi wajib tersedia. Seluruh item memakai tanggal awal organisasi yang sama dan tanggal tersebut tidak boleh sesudah jurnal terposting paling awal. Dokumen yang sudah menjadi invoice/tagihan di aplikasi ditolak, termasuk pencatatan ulang melalui alur biasa setelah masuk saldo awal.

Finance memilih mutasi bank nyata yang masuk dari pelanggan atau keluar untuk pemasok, memeriksa jumlah/sisa, lalu mengonfirmasi. Pencocokan dapat sebagian atau seluruhnya. Tanggal mengikuti bank; tidak melakukan transfer baru atau mengganti tanggal untuk menghindari batas periode. Sisa pada setiap tanggal harus benar, termasuk bila ada pembayaran setelahnya. Riwayat immutable dan permintaan ulang dengan fakta persis sama tidak menambah jurnal.

Formulir memakai tiga langkah: pilih dan isi, periksa sumber, konfirmasi dan simpan. Isian dipertahankan saat sumber berubah; periksa ulang memperbarui token dan menghapus konfirmasi. Owner membuat saldo awal; Finance/Owner mencocokkan pembayaran. Auditor membaca; Marketing ditolak. Daftar/detail bertanggal dapat dibaca tanpa mengubah catatan; tindakan selalu memeriksa sumber aktual.

## Pembukuan

| Kejadian | Jurnal |
|---|---|
| Piutang lama RpA | Debit piutang A, kredit saldo awal/ekuitas A |
| Utang lama RpP | Debit saldo awal/ekuitas P, kredit utang P |
| Penerimaan nyata RpX | Debit bank X, kredit piutang X |
| Pembayaran nyata RpY | Debit utang Y, kredit bank Y |

Saldo awal tidak membuat invoice, tagihan, penjualan, biaya, kewajiban layanan atau mutasi bank. Pencocokan memakai BankPosting yang sudah dipakai alur lain, sehingga satu mutasi tidak mempunyai kapasitas kedua. Bank/jurnal/pihak/dokumen, semua klaim bank dan jejak keputusan divalidasi dalam organisasi yang sama. Sumber rusak harus ditampilkan sebagai perlu pemeriksaan, bukan saldo nol.

Subledger penutupan menghitung piutang invoice yang berlaku ditambah sisa piutang awal; utang tagihan ditambah sisa utang awal. Neraca/jurnal tetap menjadi sumber saldo umum. Rincian saldo lama ditampilkan sebagai bagian dari total, bukan angka tambahan yang dijumlah dua kali. Tidak ada pengakuan pendapatan/biaya periode berjalan dari pencatatan awal atau pelunasannya.

## Pajak

Saldo komersial dan kas nyata tidak menentukan bruto, potongan atau masa pajak. Penerimaan piutang lama menjadi kandidat sumber bulanan tersendiri dengan kas aktual dan bruto belum ditentukan. Pemeriksa menentukan masuk/dikeluarkan atau penyesuaian waktu berdasarkan bukti; aplikasi tidak otomatis memasukkannya sebagai omzet ataupun mengecualikannya hanya karena dokumen lama. Pembayaran utang lama tidak membuat kandidat omzet.

Setiap kas pencocokan mempunyai pemeriksaan profesional atas peristiwa asli, terpisah dari keputusan klasifikasi fiskal bulanan. Kas boleh dicatat sementara tugas pajak tetap terbuka. Persetujuan fiskal/tutup buku yang bergantung padanya ditahan sampai bukti yang didukung lengkap. Tugas tidak hilang saat piutang/utang sudah lunas. Cabang tanpa pemotongan memerlukan laporan profesional yang benar-benar diterima dengan nilai nol eksplisit; potongan positif/belum jelas tetap belum didukung, bukan dianggap nol atau dibuat menjadi kredit/kewajiban pajak fiktif.

Penjelasan [DJP tentang PPh23](https://www.pajak.go.id/id/pph-pasal-2326) menyebut pembayaran, ketersediaan pembayaran dan jatuh tempo. Implikasi desain: tanggal saldo awal atau transfer saja tidak memutuskan perlakuan pajak. Sumber tersebut tidak digunakan untuk menyalin tenggat/mekanisme pelaporan lama atau membuat tarif baru. Dokumen dan keputusan perusahaan tetap diperiksa dengan sumber yang sesuai.

Laporan profesional terikat sumber kas, pihak dan PDF asli; versi sebelumnya tetap ada. Perubahan bukti/pihak/sumber membuat keputusan stale dan memperbarui persetujuan terkait. Revisi setelah periode tertutup, persetujuan bulanan, kewajiban berlanjut atau tahunan disetujui/diajukan mengikuti batas pemulihan yang dijaga.

## Bukti penerimaan yang harus dipenuhi

- Oracle publik asli pada source FIN03 harus gagal karena modul/form belum tersedia, lalu probe dengan assertion yang sama harus lulus setelah implementasi. Percobaan fixture awal yang gagal sebelum assertion tetap disimpan.
- Contoh sintetis: bank awal Rp1.000, piutang awal Rp300 dan utang awal Rp200; 9 September tetap bank1.000/piutang300/utang200/ekuitas1.100. Penerimaan120 dan pembayaran50 pada10 September memberi bank1.070/piutang180/utang150/ekuitas1.100. Tidak ada invoice/tagihan/pendapatan/biaya baru, saldo jurnal seimbang, retry tidak menambah baris.
- Uji nominal/tanggal/organisasi/peran, duplicate dokumen dua arah, sumber rusak, seluruh posisi historis, perubahan bersama, close equations dan laporan; PostgreSQL nyata untuk kompetisi residual/mutasi/closing, cleanup normal.
- Tinjauan tanpa potongan dari laporan diterima, positif/belum jelas tetap ditahan, saldo0 tetap memiliki tugas, perubahan bukti/sumber membatalkan kesegaran, kandidat AR dapat ditinjau tanpa invoice fiktif dan keputusan bulanan tidak mengganti tinjauan kas.
- Browser desktop/ponsel, Finance tanpa JavaScript, akses pembaca/marketing, stale-isian, pencocokan/baca ulang/riwayat serta laporan. Pemeriksaan kode/migrasi/dependensi dan regresi yang sesuai sebelum penerimaan.
- Pemasangan perusahaan hanya setelah sumber yang sama lulus, dengan backup dan perbandingan seluruh data/skema/ID/migrasi/unggahan. Jangan mengimpor nominal asli sebelum dokumen saldo awal tersedia. UAT staf, cloud HTTPS/provider live dan perlakuan pajak lanjutan tetap perlu bukti tersendiri.

## Batas yang tetap perlu pekerjaan

Saldo awal akun lain, rugi/kredit/kewajiban pajak historis, bukti potong pelanggan atas saldo lama, pemotongan positif, koreksi saldo awal/pencocokan yang salah dan pembalikan kas tidak boleh diselesaikan melalui jurnal atau dokumen tebakan. Semuanya memerlukan alur sumber yang dapat ditelusuri. Kontrak ini tidak mengecilkan backlog ERP; penerimaan satu alur tidak menutup kemampuan yang masih belum tersedia.
