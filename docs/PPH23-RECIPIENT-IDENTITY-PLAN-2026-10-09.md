# PPh 23: identitas penerima dan tarif yang dapat ditelusuri

Status: kontrak TAX-02 diterima sebagai [rilis terbatas](PPH23-RECIPIENT-IDENTITY-RELEASE-2026-10-09.md) pada source `e6a35cfd1f64e267286477898d28d53145766985baa2343d885b230bc3e6ffd3`. Uraian masalah awal di bawah tetap menjadi dasar penerimaan. Paket sumber omzet bulanan tidak menutup kebutuhan ini. Riset dan QA tidak memeriksa dokumen atau status pajak IIEF, OSEE, maupun pemasok sebenarnya.

## Masalah yang harus diselesaikan

`Party.tax_id` saat ini berupa teks bebas. `ObligationForm` dan keputusan tagihan memilih tarif jasa 2% atau penghasilan tertentu 15% tanpa register status identitas penerima. Laporan profesional mengikat keputusan dan PDF, tetapi aplikasi belum mengikat keputusan tarif ke identitas penerima yang berlaku pada masa itu. Kolom kosong tidak membuktikan bahwa penerima tidak mempunyai NPWP.

Jejak kode yang perlu diubah pada tahap implementasi:

| Kontrak sekarang | Celah yang masih ada |
| --- | --- |
| `finance/models.py:Party.tax_id` | Teks bebas tanpa jenis penerima, hasil pemeriksaan, atau tanggal berlaku. |
| `taxes/rules.py:calculate_tax` | Hanya tarif tetap 2%/15%; tidak menerima keputusan identitas atau faktor kenaikan. |
| `taxes/workflow.py:prepare_obligation` | Arah payable dapat dibuat tanpa penerima eksplisit; tidak mengikat identitas penerima atau sumber hukum per masa. |
| `taxes/workflow.py:review_bill_tax` | Laporan mengikat tagihan dan keputusan dasar/tarif, tetapi belum mengikat status identitas pemasok. |
| `taxes/workflow.py:subject_snapshot` | Snapshot tagihan mengikat `supplier_id`; perubahan identitas pemasok tidak masuk digest. Snapshot kewajiban juga belum membawa identitas penerima/pemotong. |
| `taxes/workflow.py:verify_obligation_evidence` | Mencocokkan NPWP perusahaan, masa, nominal dan bukti. Belum memverifikasi identitas kedua pihak serta objek bukti potong sebagai kontrak tersendiri. |

## Dasar primer dan batas kesimpulan

- UU PPh Pasal 23 membedakan kelompok objek bertarif 15% dan 2%. Ayat (1a) menaikkan tarif 100% terhadap penerima yang tidak memiliki NPWP: faktor 2, bukan tambahan 100 poin persentase. Pasal 23 juga membatasi penerima dan pihak pemotong; waktu penghasilan dibayar, disediakan untuk pembayaran atau jatuh tempo perlu diperiksa. Pasal 21/26 dan pengecualian Pasal 23 tidak boleh dilewati hanya karena transaksi berlabel jasa. [JDIH Kemenkeu — susunan UU perpajakan, UU PPh Pasal 21, 23 dan 26, halaman PDF 225–232](https://jdih.kemenkeu.go.id/api/download/50e4f976-7d46-4bbc-9230-e213a1674197/SDSN%20Pajak.pdf)
- PMK 141/2015 Pasal 1 mengatur jasa lain, dasar bruto tanpa PPN, pengecualian untuk penghasilan yang telah dikenai PPh final, dan komponen tertentu yang dapat dikeluarkan dari bruto dengan bukti sesuai ketentuan. Daftarnya mencakup kursus/pelatihan, sertifikasi dan tester. Ini memberi kandidat klasifikasi; tidak menetapkan otomatis perlakuan TOEFL atau kontrak IIEF. [DJP — PMK 141/2015, Pasal 1](https://www.pajak.go.id/index.php/id/peraturan/jenis-jasa-lain-sebagaimana-dimaksud-dalam-pasal-23-ayat-1-huruf-c-angka-2-undang-0)
- PMK 81/2024 Pasal 16–18 membedakan NIK yang diaktivasi sebagai NPWP dan identitas perpajakan lain, termasuk NIK tervalidasi untuk keadaan yang diatur. Peraturan berlaku mulai 1 Januari 2025. [DJP — PMK 81/2024, Pasal 16–18 dan 484](https://stats.pajak.go.id/index.php/en/node/113110)
- PER-7/PJ/2025 Pasal 2 menggunakan NIK yang diaktivasi untuk orang pribadi Penduduk dan NPWP 16 digit yang dihasilkan sistem DJP untuk kategori lainnya. Pasal 7–8 juga mengatur identitas perpajakan yang belum diaktivasi dan pencantuman identitas pihak yang dipotong. Pasal 15 memisahkan validasi kependudukan dari aktivasi NPWP. Karena itu, validasi, integrasi dan aktivasi harus disimpan sebagai fakta yang berbeda. [DJP — PER-7/PJ/2025, Pasal 2, 7–8 dan 15](https://stats.pajak.go.id/id/peraturan/petunjuk-pelaksanaan-administrasi-nomor-pokok-wajib-pajak-pengusaha-kena-pajak-objek)
- PER-11/PJ/2025 Pasal 18 dan 21 meminta identitas NPWP/NIK dan nama untuk pihak dalam negeri pada bukti unifikasi, serta identitas berbeda untuk pihak luar negeri. Pasal 129 mengatur pembulatan rupiah penuh. Ketentuan ini mendasari kebutuhan data; bukan bukti bahwa CSV aplikasi diterima Coretax. [DJP — PER-11/PJ/2025](https://www.pajak.go.id/index.php/id/peraturan/ketentuan-pelaporan-pajak-penghasilan-pajak-pertambahan-nilai-pajak-penjualan-atas-0)
- PENG-6/PJ.09/2024 angka 2, 7–8 menjelaskan bahwa kenaikan tarif tidak dikenakan ketika identitas orang pribadi Penduduk memakai NIK yang diadministrasikan Dukcapil dan terintegrasi dengan sistem DJP; aktivasi NPWP dibahas terpisah. Pengumuman itu memiliki konteks format administrasi 2024. Gunakan sebagai penjelasan perlakuan integrasi bersama dasar administrasi yang berlaku, tidak sebagai satu-satunya spesifikasi format 2026. [DJP — PENG-6/PJ.09/2024](https://stats.pajak.go.id/id/pengumuman/penggunaan-nomor-pokok-wajib-pajak-pada-sistem-administrasi-perpajakan)
- Untuk pekerjaan/jasa orang pribadi, PMK 168/2023 harus masuk pemeriksaan rute PPh 21/26. Untuk residensi pajak, PER-23/PJ/2025 menjadi sumber terkini yang perlu diikat pada keputusan terkait. Kewarganegaraan, kepemilikan NIK dan NPWP tidak menggantikan pemeriksaan status subjek pajak. [DJP — PMK 168/2023](https://www.pajak.go.id/index.php/id/peraturan/petunjuk-pelaksanaan-pemotongan-pajak-atas-penghasilan-sehubungan-dengan-pekerjaan-jasa-1), [DJP — PER-23/PJ/2025](https://www.pajak.go.id/id/peraturan/penentuan-subjek-pajak-dalam-negeri-dan-subjek-pajak-luar-negeri-0)

Riset juga membaca perubahan PMK 81 melalui [PMK 54/2025](https://www.pajak.go.id/index.php/id/peraturan/perubahan-ketiga-atas-peraturan-menteri-keuangan-nomor-81-tahun-2024-tentang-ketentuan) dan teks [PMK 1/2026](https://jdih.kemenkeu.go.id/api/download/b5f99bff-f689-4e4f-ae3a-7c6a9a4cfe8a/2026pmkeuangan001.pdf). Jangan menyimpulkan perubahan tarif atau aturan identitas hanya dari metadata bahwa suatu peraturan telah diubah. Publikasi hukum dalam aplikasi tetap memerlukan tinjauan operator; riset dokumen ini tidak mengaktifkan sumber.

## Keputusan tarif yang boleh didukung

Semua baris berikut hanya berlaku setelah objek, arah, dasar bruto dan waktu terutang dinyatakan sesuai PPh 23 oleh laporan profesional. Ini merupakan batas implementasi yang diusulkan, bukan klasifikasi transaksi perusahaan.

| Hasil pemeriksaan identitas penerima pada tanggal terkait | Faktor identitas | Tarif efektif untuk objek dasar 2% / 15% |
| --- | --- | --- |
| NPWP terdaftar yang terverifikasi, termasuk NIK sebagai NPWP yang terverifikasi | 1 | 2% / 15% |
| NIK orang pribadi Penduduk terintegrasi dengan DJP yang terverifikasi dan perlakuan standar dikonfirmasi profesional untuk masa itu | 1 | 2% / 15%; simpan bukti integrasi dan dasar angka 7 PENG-6 bersama sumber administrasi masa itu |
| Tidak memiliki NPWP yang dinyatakan dan didukung bukti profesional; bukan kasus NIK terintegrasi di atas | 2 | 4% / 30% |
| Nomor kosong, nomor hanya lolos format, KTP saja, atau status belum diperiksa | Belum ditentukan | Tahan persetujuan dan pembayaran potongan; tampilkan tugas melengkapi pemeriksaan |
| NIK hanya tervalidasi/registration-only tanpa kesimpulan integrasi dan perlakuan tarif | Belum ditentukan | Minta bukti dan kesimpulan profesional; jangan menganggap semua NIK berfaktor 1 atau semua yang belum aktif berfaktor 2 |
| NPWP nonaktif, sudah dihapus, atau bukti perubahan status bertanggal berbeda | Perlu pemeriksaan spesifik | Status nonaktif bukan bukti otomatis tidak mempunyai NPWP; gunakan tanggal dan dokumen resmi |

Untuk Rp500.000 dasar yang telah dikonfirmasi, oracle terbatasnya adalah Rp10.000/20.000 pada objek 2% dengan faktor 1/2, dan Rp75.000/150.000 pada objek 15%. Contoh ini menguji aritmetika, bukan menetapkan dasar bruto invoice nyata. Harga pemasok “setelah pajak”, nilai transfer neto, dan harga jual mitra tidak otomatis menjadi dasar pajak atau alasan gross-up.

## Kontrak minimum

1. Buat keputusan identitas tetap, berversi dan berlaku menurut tanggal: organisasi, target penerima tepat, nama legal, jenis orang pribadi/badan, status SPDN/SPLN/BUT/belum diperiksa, status Penduduk untuk bentuk NIK, jenis/nomor identitas, status registrasi/validasi/integrasi/aktivasi, tanggal mulai/akhir berlaku, referensi dan hash byte dokumen privat, pemeriksa, tanggal pemeriksaan, laporan profesional serta alasan. Nilai awal selalu belum diperiksa. Pisahkan status residensi pajak dari kategori identitas Penduduk.
2. Untuk `payable`, penerimanya `Bill.supplier` atau Party eksplisit dengan bukti sumber pada transaksi manual; OSEE adalah pemotong. Untuk `receivable`, penerimanya organisasi OSEE; pelanggan pada invoice adalah pemotong. NIK direktur/PIC tidak pernah menggantikan NPWP PT. Keputusan identitas badan OSEE perlu snapshot tersendiri; satu kolom terbaru pada profil tidak cukup untuk riwayat masa.
3. Nomor dengan format sah hanya lolos pemeriksaan format. Status terdaftar/terintegrasi atau tidak memiliki NPWP memerlukan bukti dan keputusan pemeriksa yang tepat; tidak ada verifikasi DJP otomatis yang diklaim.
4. Keputusan objek tetap diperlukan. Pembayaran kepada orang pribadi, tutor, pihak luar negeri, transaksi final atau bukan objek tidak otomatis masuk PPh 23 karena berlabel jasa.
5. Kalkulator baru menerima `rule_code`, `base`, `tax_point_date`, `period`, `recipient_identity_decision`, keputusan objek dan snapshot hukum yang ditinjau. Tolak jika masa tidak memuat tanggal terutang atau identitas tidak berlaku pada tanggal itu. Keluarkan dasar awal/dasar dibulatkan, tarif dasar, faktor identitas, tarif efektif, nominal, versi kalkulator, ID/hash identitas, keputusan objek dan sumber hukum. Panggilan lama tanpa konteks tetap boleh membaca sejarah; tidak boleh menyiapkan atau menyetujui kewajiban PPh 23 baru melalui jalan pintas.
6. Kewajiban dan keputusan tagihan mengikat snapshot penerima, pemotong, objek, tanggal terutang, identitas, versi/hashes hukum dan byte dokumen. Perubahan relevan membuat draf memerlukan pemeriksaan baru. Dua identitas efektif bertentangan pada tanggal yang sama harus ditolak. Riwayat yang sudah disetujui, dibayar atau dilaporkan tetap utuh dan memerlukan koreksi formal; jangan mengubah tarifnya saat data master diperbarui.
7. Informasi identitas sensitif tidak ditambahkan ke konteks OpenRouter atau modul Marketing. Tampilan daftar cukup menunjukkan penerima dan status pemeriksaan; nomor lengkap hanya pada layar/dokumen pajak dengan akses yang sesuai.

Layanan yang diusulkan: `prepare_recipient_identity`, `approve_recipient_identity`, `resolve_recipient_identity(organization, recipient, tax_point_date)` dan `calculate_pph23(...)`. Finance menyiapkan; Owner mencatat persetujuan berdasarkan laporan profesional yang tepat. Gunakan kunci organisasi/penerima yang konsisten, transaksi atomik, fingerprint/versi halaman serta identitas permintaan untuk retry. Persetujuan mengikat laporan ke snapshot dan keputusan yang tepat. Nomor dinormalisasi sebagai teks; pertahankan identitas dokumen historis. Konversi NPWP lama ke format lain tidak membuktikan pemilik atau status dan tidak boleh dilakukan tanpa pemetaan terverifikasi.

Verifikasi bukti potong pelanggan harus mencocokkan OSEE sebagai penerima, pelanggan sebagai pemotong, nomor bukti/status, masa, objek, dasar dan potongan terhadap keputusan yang terikat. Bukti salah identitas/tarif menjadi kasus perbedaan yang perlu ditangani, bukan alasan untuk membuat uang bank atau kredit pajak tambahan. Jalur net receipt dan kredit potong yang datang setelah pencocokan bank tetap memakai satu bukti sekali saja; kas, bruto pelunasan piutang dan kredit pajak tetap terpisah.

Rute PPh 21, PPh 26, final, pengecualian/SKB atau belum terklasifikasi disimpan sebagai hasil review beserta alasan. Jika kalkulator/rute belum tersedia, tampilkan langkah penanganan profesional yang jelas. Jangan mengubahnya menjadi `no_withholding` umum agar tagihan dapat dibayar atau menyamakan final OSEE dengan kewajiban pemotongan pemasok.

## Alur staf

Finance membuka tagihan, melihat penerima dan tugas **Lengkapi identitas pajak**, mengunggah dokumen, lalu mencatat hasil pemeriksaan profesional. Pilihan yang belum sah menampilkan alasan dan langkah berikutnya; aplikasi tidak meminta staf menebak tarif dari kolom kosong. Owner mencatat persetujuan sesuai alur bukti yang sudah ada. Draf memakai identitas, objek, dasar dan tarif yang terikat pada keputusan tersebut.

## Uji penerimaan yang diperlukan

- Nomor kosong atau hanya format yang sah tetap belum terkonfirmasi; tidak menghasilkan pemilihan tarif standar atau kenaikan otomatis.
- Pemasok dan OSEE sebagai penerima tidak tertukar; identitas organisasi lain dan periode yang tidak berlaku ditolak.
- Status NPWP terverifikasi, NIK terintegrasi terverifikasi, dan tidak memiliki NPWP yang terdokumentasi menghasilkan faktor 1/1/2 hanya untuk objek dan masa yang ditinjau; uji oracle Rp500.000 di atas dan pembulatan pada batas pecahan rupiah.
- Orang pribadi/luar negeri/objek yang belum diklasifikasikan tidak didorong ke aturan jasa PPh 23 hanya untuk menutup formulir.
- Draf lama tanpa bukti identitas tidak menjadi sah melalui migrasi; riwayat lama tetap terbaca. Perubahan dokumen/identitas membuat persetujuan baru gagal sampai diperiksa ulang.
- Retry dan dua staf bersamaan tidak membuat keputusan aktif yang bertentangan. CSV dan PDF membawa versi yang sama dengan draf; Finance/Owner mempunyai akses, Marketing ditolak.
- Identitas berubah tengah bulan: transaksi sebelum/sesudah perubahan mengikat versi yang tepat; perubahan tanggal yang relevan membuat draf stale. Tanggal tagihan/bank/pelaksanaan tidak otomatis dipilih sebagai waktu terutang tanpa dasar profesional.
- Rute salah PPh 21/26/final, nomor orang pribadi pada PT, identitas penerima/pemotong tertukar, dokumen berubah, atau laporan untuk versi lama ditolak sebelum persetujuan/pembayaran baru.
- PPh 23 terverifikasi yang telah dilunasi tidak berubah saat identitas direvisi; koreksi formal belum didukung tidak ditutupi oleh draf kedua atau jurnal buatan. Jalur net receipt/kredit terlambat tetap tanpa kas semu dan tanpa pemakaian ganda.

## Hal yang masih memerlukan bukti sebelum ekspor/pelaporan resmi

1. Teks normatif lengkap dan lampiran terbaru PER-12/PJ/2026 serta format impor eBupot yang berlaku pada tanggal penyampaian perlu ditinjau sebelum implementasi ekspor resmi. Materi edukasi dan artikel DJP mengonfirmasi perubahan mulai 1 Oktober 2026, tetapi bukan pengganti verifikasi skema/kolom/kode objek. Untuk tahap identitas, keluaran tetap kertas kerja dan pencocokan manual; jangan mengklaim XML/CSV Coretax tervalidasi. [DJP — penjelasan PER-12/PJ/2026](https://www.pajak.go.id/id/artikel/goodbye-skema-delta-coretax-kini-terapkan-skema-replace-mulai-1-oktober-2026-12pj2026)
2. Bukti yang menunjukkan suatu NIK hanya tervalidasi versus terintegrasi versus aktif perlu diperiksa secara manual melalui dokumen/hasil layanan resmi yang sah, disertai kesimpulan profesional. Belum ada koneksi verifikasi DJP yang disetujui. Pengumuman portal pemadanan versi 2 menyebut penggunaan bukti PPh 21; jangan memperluas kewenangan atau hasilnya menjadi API verifikasi PPh 23 tanpa bukti. [DJP — portal pemadanan versi 2](https://www.pajak.go.id/index.php/id/pengumuman/layanan-portal-pemadanan-nik-npwp-dan-simulator-terpandu-coretax-djp)
3. Kasus badan tanpa NPWP yang terdokumentasi dapat memerlukan tarif lebih tinggi pada kertas kerja, tetapi identitas resmi yang diterima Coretax tidak boleh diganti nomor dummy atau NPWP OSEE. Pemetaan format resmi harus diselesaikan sebelum pelaporan; ini berbeda dari keputusan aritmetika tarif.
4. SKB, fasilitas final, treaty/PPh 26, komponen royalty/lisensi, gross-up, pihak agen/prinsipal, dan koreksi kewajiban/bukti/SPT yang sudah disetujui atau dilaporkan memerlukan kontrak serta dokumen perusahaan dan aturan rute masing-masing. Fitur identitas tidak menutup semua jalur tersebut.

Rencana ini tidak menetapkan identitas atau perlakuan pemasok IIEF maupun pelanggan OSEE. Penetapan tersebut harus berasal dari kontrak dan dokumen perusahaan yang sebenarnya.

## Batas dan kontrak batch implementasi

Persiapan baru yang memakai kalkulator identitas ini dibatasi sejak 22 Mei 2025 sampai masa berjalan yang didukung versi kalkulator. Ini mengikuti batas aturan pembulatan dan administrasi yang telah ditinjau; riwayat sebelumnya tetap terbaca, tetapi tidak dianggap memenuhi pemeriksaan baru. Ekspor resmi Coretax, pembetulan bukti/SPT, dan kalkulator PPh21/26/final pemasok tetap di luar batch dan harus mempunyai alasan penanganan yang jelas.

Finance menyiapkan `RecipientTaxIdentity` yang tetap. Owner mencatat laporan profesional melalui `RecipientIdentityApproval`; nomor dan dokumen tidak berubah setelah disimpan. Perubahan memakai penerus yang jelas dan tanggal berlaku, tanpa menimpa rentang lama. Keputusan yang berkonflik ditolak. Persetujuan berstatus belum terkonfirmasi boleh mencatat hasil pemeriksaan, tetapi tidak menjadi identitas siap pakai dalam kalkulator.

Melengkapi keputusan yang sebelumnya belum terkonfirmasi tidak boleh memaksa staf mengarang tanggal perubahan. Penerus boleh memakai tanggal mulai yang sama hanya ketika versi sebelumnya disetujui dengan faktor belum ditentukan, penerus mempunyai kesimpulan tarif terdokumentasi, dan tidak ada kewajiban disetujui, bukti potong, atau pencatatan uang yang terikat pada versi lama. Riwayat tetap tersimpan; dua penerus yang berkonflik ditolak. Versi dengan tarif yang sudah ditetapkan tetap memerlukan tanggal perubahan yang sah atau alur koreksi formal. Draf keliru yang belum disetujui dapat ditinggalkan dan disiapkan ulang; tidak memperoleh persetujuan atau mengubah sejarah secara otomatis.

Laporan profesional yang tetap tidak diberi kedaluwarsa otomatis 180 hari; tanggal laporan dan fakta berlaku tetap tersimpan. Kebijakan tinjauan 180 hari berlaku pada sumber hukum aplikasi, bukan klaim bahwa laporan profesional kedaluwarsa menurut hukum. Pembaruan tinjauan sumber tanpa perubahan isi normatif boleh mempertahankan penggunaan identitas lama setelah hash/publikasi asli dan head terkini diperiksa. Perubahan isi atau rentang aturan harus ditinjau; aplikasi tidak mengganti keputusan lama diam-diam.

Kewajiban baru mengikat identitas penerima, tanggal dan dasar waktu terutang (`paid`, `available`, atau `due`), keputusan objek, pihak pemotong, versi hukum dan hasil perhitungan. Untuk potongan pemasok, identitas PT OSEE sebagai pemotong juga harus mempunyai persetujuan yang berlaku. Untuk potongan pelanggan, PT OSEE adalah penerima; nama/nomor pelanggan sebagai pemotong dicocokkan dengan dokumen dan invoice, tanpa menganggap teks `Party.tax_id` sebagai verifikasi.

`assert_pph23_obligation(obligation, require_current=True)` menjadi penjagaan bersama untuk persetujuan, bukti dan pencatatan uang baru. Pelunasan neto pemasok, setoran potongan, penerimaan neto pelanggan, kredit terlambat dan penggunaan kredit pada pengakuan tahunan harus melewatinya. Retry identik atas pencatatan yang sudah selesai tetap mengembalikan hasil lama dan tidak membuat uang baru.

Batch ini mendukung pelunasan neto pemasok penuh, penerimaan neto pelanggan dan kredit terlambat dari alokasi bank yang didukung. Uang muka pemasok sebelum tagihan, pemotongan saat akrual dengan pelunasan parsial/lintas masa, bukti yang berasal dari penerapan uang muka pelanggan, dan koreksi formal tetap mempunyai backlog terpisah. Tanggal terutang tidak diturunkan diam-diam dari tanggal transfer.

Sumber hukum baru hanya disiapkan sebagai versi belum terpublikasi. Publikasi masih merupakan tugas operator global melalui management command; layar staf harus menjelaskan bahwa sumber perlu ditinjau dan siapa yang menindaklanjuti, tanpa meminta staf menjalankan perintah pengembang. Alur UI publikasi dengan batas kewenangan yang benar tercatat sebagai UX-04. Pengujian sintetis tidak mengaktifkan sumber atau identitas pajak perusahaan sebenarnya.

Uji RED independen `recipient-independent-legacy-red-20261009-1` mereproduksi bahwa persiapan PPh23 lama dapat diterima tanpa identitas penerima. Perbaikan harus menutup jalur tersebut, bukan hanya menambah kolom pada UI. Bukti QA final akan memakai data sintetis, provider dimatikan dan database terpisah; data perusahaan tidak dijadikan fixture.
