# Penarikan dan penggantian sumber iklan marketing

**MKT-03 lulus penerimaan `READY_FOR_SCOPE` dan sudah diterapkan pada preview lokal.** Hasil ini mencakup penarikan sumber iklan serta penggantian satu baris lama dengan satu baris sumber nyata, dengan riwayat tetap tersedia. Migrasi dan pemeriksaan sesudah startup mempertahankan 975 catatan asli pada 123 tabel. Ini tidak menyatakan seluruh ERP siap digunakan perusahaan. Celah dependency migrasi yang ditemukan setelah gate juga masih **OPEN** dan dijelaskan di bawah.

Build source/tes: `e32c5d84aa804d04554c77f66b817a72e14a61e265d3ec1162c51513473eeedd`. Gate: `.local/quality-20261009/marketing-ad-final-3/check.json`; laporan: `report.md`; pemeriksaan koordinator: `root-review.md` pada direktori yang sama. Enam kasus wajib lulus, lima temuan tercatat terselesaikan, tanpa gap/blocker dalam kontrak gate yang dibekukan. Environment pengujian memakai SQLite, PostgreSQL 17 dan Chrome dengan data sintetis terpisah; provider dimatikan dan `company_data_used=false`.

## Hasil untuk staf

Dari daftar kampanye, staf membuka **“Lihat laporan →”**, lalu memilih penarikan atau penggantian. Form menampilkan sumber lama, meminta alasan dan bukti, dan memperlihatkan data pengganti sebelum disimpan. Referensi dokumen serta pencatat asli tetap dapat ditelusuri; staf tidak perlu membuat referensi dokumen baru untuk membetulkan sumber lama.

Penggantian memperbaiki kampanye, tanggal, nama iklan, audiens, wilayah atau tingkat rincian yang keliru. Koreksi angka pada sumber aktif tetap memakai alur koreksi yang tersedia. Penggantian yang bertabrakan dengan sumber aktif ditolak seluruhnya: sumber lama tetap aktif dan masukan formulir tetap terlihat. Bila sumber berubah saat formulir terbuka, staf memilih **“Periksa versi terbaru”**, memeriksa kembali perbandingan, lalu mengonfirmasi lagi sebelum simpan.

Hanya sumber aktif dihitung dalam biaya, tayangan, klik dan rasio. Menarik laporan Rp100 tidak membuktikan biaya sebenarnya Rp0. Nilai belum diketahui dan nol yang berbukti tetap berbeda. Kelengkapan sumber serta evaluasi KPI perlu diperiksa ulang; review lama tetap tersimpan dengan fakta lama. Penggantian bernilai sama juga membatalkan review atau konfirmasi rekomendasi yang memakai identitas sumber sebelumnya.

Owner/direktur, Finance dan Marketing yang aktif dapat memakai tindakan sesuai batas organisasi/peran. Auditor dapat membaca hasil dan riwayat tanpa akses perubahan. Alur pemulihan sumber ini tidak membuat jurnal, tagihan, pembayaran atau mutasi bank Finance.

## Perilaku yang sudah diuji

- Satu pendahulu menerima satu keputusan penarikan atau satu pengganti. Penggantian, pengarsipan sumber lama, keputusan dan audit disimpan atomik.
- Retry dengan permintaan asli yang sama mengembalikan hasil yang sama; perubahan payload, bukti versi, actor atau request yang berbenturan ditolak. Perlombaan PostgreSQL nyata menerima satu hasil yang konsisten.
- Identitas dokumen, pembuat asli, nilai lama, alasan, bukti dan rantai riwayat tetap dapat dibaca. Impor ulang dokumen lama tidak mengaktifkan kembali sumber yang sudah ditarik atau menggandakan pengganti.
- Perubahan sumber memengaruhi scope lama dan baru. Membuat lalu menarik sumber hingga kumpulan aktif kosong tetap membatalkan pemeriksaan lama; hasil kosong tidak menyembunyikan perubahan riwayat.
- Kelengkapan/KPI, ringkasan Direktur, API/CSV dan identitas saran AI memakai sumber aktif dan provenance yang konsisten. Konfirmasi rekomendasi dengan sumber lama ditolak; masukan pekerjaan tetap ditampilkan dan tidak ada pekerjaan yang dibuat dari konfirmasi kedaluwarsa.
- Batas organisasi, pengguna aktif, peran, CSRF, versi dan fingerprint ditegakkan server. Recovery text, referensi dokumen, ID serta fingerprint tetap lokal; pengujian ini tidak memperluas payload privat ke AI.

Penggantian satu total menjadi beberapa baris dalam satu keputusan **belum termasuk**. Pengganti satu total menjadi satu rincian tidak menyatakan semua rincian lengkap. Rincian lain dari dokumen nyata dapat ditambahkan melalui impor CSV atomik yang sudah tersedia, lalu cakupan sumber diperiksa ulang.

## Bukti penerimaan akhir

Seluruh perintah akhir mencatat source yang sama sebelum dan sesudah eksekusi serta exit 0. Bukti asli berada di `.local/fix-2026-10-02/verification/`; salinan yang terikat hash disimpan pada gate ketiga.

| Pemeriksaan | Hasil aktual |
|---|---|
| SQLite, `marketing-ad-final-sqlite-20261009-3` | 924 kasus dijalankan: 882 lulus dan 42 khusus PostgreSQL dilewati; database tes dibersihkan normal |
| PostgreSQL, `marketing-ad-final-postgres-20261009-3` | 924 lulus, tanpa skip; database tes dihapus normal dan perintah exit 0, termasuk kasus konkurensi nyata |
| Independen, `marketing-ad-final-independent-20261009-3` | 7 lulus: dua reproduksi publik asli tanpa perubahan ekspektasi, serta lima kasus independen untuk rantai riwayat, sumber kosong, nilai sama, rollback dan konflik impor terlambat |
| Browser, `marketing-ad-final-browser-20261009-3` | 4 alur pada lebar 1440/390, 78 keadaan halaman, 14 PNG; tidak ada error browser atau overflow horizontal |
| Kualitas, `marketing-ad-final-quality-20261009-3` | Lima pemeriksaan lulus: Django check, migration drift dry-run, Ruff kritis E9/F63/F7/F82, kompilasi dan konsistensi dependensi |
| Batas akses dan review | Bukti organisasi/peran/CSRF, input kedaluwarsa, pemulihan atomik dan pembatasan payload AI ditinjau bersama hasil native/browser; kasus security gate lulus |

Browser memeriksa data yang tersimpan pada keempat fixture: referensi dan pencatat asli tetap, tepat satu keputusan per alur, sumber aktif benar, kelengkapan perlu konfirmasi ulang, serta keadaan Finance tidak berubah. Penarikan nol yang terdokumentasi dan penarikan nilai belum diketahui diuji terpisah. Dua JSON hasil dan 14 screenshot menjadi bukti; fixture kredensial serta database browser tidak disertakan dalam artefak yang dibagikan.

Pemeriksaan visual koordinator mencakup riwayat penarikan ponsel, penggantian kedaluwarsa ponsel dan hasil penggantian desktop. Label/tindakan terbaca dan proposal tetap tampil saat stale. Ini tetap berbeda dari UAT pertama kali oleh staf nyata; klaim “tanpa learning curve” belum dibuktikan oleh browser otomatis.

## Percobaan gagal tetap disimpan

Gate pertama, `marketing-ad-final-1` pada `65505455…`, tetap **`NEEDS_EVIDENCE`**. SQLite menjalankan 923 kasus dengan 881 lulus/42 skip. Semua 923 assertion PostgreSQL lulus, tetapi perintah exit 1 saat DROP karena dua koneksi masih menggunakan database tes. Itu bukan perintah yang lulus. Probe yang mewarisi dua metode tes pajak lama tanpa mengganti assertion membuktikan `close_old_connections()` masih meninggalkan dua koneksi worker sehat pada setiap kasus. Finalizer diperbaiki menjadi penutupan eksplisit `connections.close_all()`; metode bisnis/aturan pajak tidak diubah. Probe yang sama kemudian menemukan nol koneksi hidup sebelum safety cleanup. Gate ketiga menyediakan seluruh suite baru dengan lifecycle normal.

Gate kedua, `marketing-ad-final-2` pada `d6151585…`, tetap **`NOT_READY`**. SQLite menjalankan 923 kasus dengan 880 lulus, satu gagal dan 42 skip. Fixture antrean harian menjadwalkan dua jam dari sekarang tetapi selalu mengharapkan hari ini; metode asli lulus pada 10.00 WIB dan gagal pada 23.30 WIB. Kesalahan setup probe jam pertama disimpan terpisah dan tidak dianggap reproduksi cacat produk. Perbaikan memakai jam fixture tetap, mempertahankan body/assertion HTTP lama, serta menambah pengujian deadline UTC yang melintasi tengah malam Jakarta. Browser dan suite PostgreSQL pada percobaan kedua tidak dijalankan; tidak dihitung sebagai bukti lulus.

Sesudah suite PostgreSQL ketiga lulus, helper koordinator untuk penghentian cluster exit 1 pada pemeriksaan observer: Windows PowerShell 5.1 menampilkan proses yang sudah tidak ada sebagai `{}`, sedangkan observer mengharapkan null. Stop asli telah mengembalikan exit 0. Kegagalan helper tetap tercatat sebagai **MKT03-PG-OBSERVER-01**. Reconciliation baca-saja dengan null eksplisit membuktikan proses, listener 15445 dan PID file tidak ada serta `pg_ctl status` menyatakan server tidak berjalan. Stop tidak diulang; PostgreSQL sistem yang tidak terkait tetap terlindungi. Ini tidak mengubah hasil perintah native PostgreSQL yang memang exit 0.

Dua kegagalan publik awal karena form tarik/ganti belum ada tetap disimpan; replay memakai probe dan ekspektasi yang sama. Kegagalan fixture migrasi awal juga tetap tercatat: string in-memory dibandingkan dengan Decimal tersimpan. Fixture dibaca ulang dari database sebelum mengambil baseline, tanpa melonggarkan perbandingan semua nilai lama.

## Penerapan lokal dan pemeriksaan sesudah startup

Receipt `.local/quality-20261009/marketing-ad-company-preservation-20261009T163558Z.json` mencatat exit 0 dan sole target `marketing.0009_ad_observation_recovery`. Receipt sesudah startup `.local/quality-20261009/marketing-ad-preview-runtime-original-preservation-20261009-1.json` berstatus `pass` dan mengikat source yang sama. Hasil aktual di bawah berasal dari kedua receipt tersebut, bukan dari fixture QA atau rilis anggaran sebelumnya.

Backup asli: `.local/backups/before-marketing-ad-recovery-20261009T163558Z.zip`, SHA256 `6fbe09c8195364c48e2ba5e2e18022123288aad6dee1d8d3454f60a471a4bdf3`. Receipt `.local/quality-20261009/marketing-ad-preview-backup-20261009T163558Z.json` mengikat source e32, baseline catatan dan schema asli dengan stamp yang sama.

Percobaan referensi SQLite terisolasi awal gagal pada Marketing0005 dengan `Related model 'director.decision' cannot be resolved`, sebelum helper mencapai jalur migrasi perusahaan. Log asli `.local/quality-20261009/marketing-ad-reference-before-migrate-20261009T163558Z.log` tetap disimpan. Continuation yang dibatasi kemudian berhasil menggunakan arsip, baseline catatan dan schema asli tersebut. Receipt menegaskan `original_archive_and_baseline_reused=true` dan `original_backup_receipt_unchanged=true`; tidak dibuat baseline baru untuk menyamarkan kegagalan awal. Referensi kosong memakai persis seluruh histori migrasi asli, tanpa Marketing0009 sampai perbandingan historis selesai. Ini memperbaiki jalur verifikasi referensi, bukan dependency source yang masih terbuka.

| Bukti aktual | Hasil |
|---|---|
| Continuation referensi | Berhasil; arsip/baseline asli dipakai ulang, receipt backup asli tidak diubah, salinan sementara milik helper dibersihkan |
| Migrasi perusahaan | Exit 0; hanya Marketing0009 diterapkan, satu catatan histori migrasi ditambahkan |
| Source sebelum/sesudah serta runtime | Tetap `e32c5d84…` |
| Tabel/catatan asli | 123 tabel dan 975 catatan dipertahankan; proyeksi seluruh nilai lama dan metadata kolom asli tetap |
| Schema | Schema lama yang tidak terkait tetap; schema Ad asli/target diperiksa terhadap referensi historis dan target yang tepat |
| Penambahan yang diizinkan | Lima definisi metadata framework dan satu catatan histori migrasi; nol catatan bisnis baru |
| Metadata Ad dan recovery | Tiga kolom baru pada seluruh Ad lama tetap NULL; register recovery baru kosong |
| Hak akses dan sumber perusahaan | Grant, membership, fakta pajak dan dokumen privat tetap; seluruh nilai Finance tercakup dalam proyeksi catatan asli yang tidak berubah |
| Integritas | Integritas database serta foreign key sesudah startup lulus |
| Startup lokal | Server PID 37604, parent/peluncur PID 48252, command workspace OSEE pada `127.0.0.1:8001`, mulai `2026-10-09T16:51:12.5521868Z` |
| HTTP anonim | 15 pemeriksaan lulus; health/readiness/login dan tiga aset berstatus 200; sembilan rute marketing/Direktur/modul mengarah ke login dengan 302 |
| Preservasi sesudah startup | Arsip/baseline asli, 123 tabel/975 catatan, metadata, schema, integritas, hak akses dan dokumen diverifikasi kembali; register baru tetap kosong |

Pemeriksaan runtime dilakukan pada `2026-10-09T16:51:25.486239+00:00`. URL preview yang diperiksa adalah [Marketing OSEE lokal](http://127.0.0.1:8001/marketing/campaigns/). Pemeriksaan anonim tidak masuk ke akun atau membaca catatan perusahaan terautentikasi, tidak mengubah data bisnis, dan tidak menghubungi provider. Logo OSEE dan dua aset UI berstatus 200. Preview ini tetap lingkungan pengembangan lokal; belum menjadi deployment cloud/domain/HTTPS perusahaan.

Rollback sesudah rantai pengganti dibuat harus memakai pemulihan arsip yang diverifikasi. Reverse migration buta dapat berbenturan dengan keunikan referensi lama; rollback tidak dinyatakan aman hanya karena perintah migrasi tersedia.

## Celah dependency migrasi yang masih OPEN

Pembaruan 10 Oktober: status OPEN di bagian ini adalah temuan pada checkpoint e32. INF-01 kemudian diselesaikan pada ad9 dalam [batch dependensi terpisah](MARKETING-MIGRATION-DEPENDENCY-RELEASE-2026-10-10.md), termasuk target kosong SQLite/PostgreSQL dan pemeriksaan histori perusahaan read-only. Gate MKT-03 historis tidak diubah atau diperluas.

`marketing/migrations/0005_performance_workflow.py` menambahkan FK ke `director.Decision` dan `director.Budget`, tetapi tidak mendeklarasikan dependency `director.0001_initial`. Migrasi penuh semua app dapat lulus karena Director ikut lebih dahulu; itu tidak membuktikan closure untuk target Marketing saja. Kegagalan referensi terisolasi awal membuktikan jalur Marketing0008 pada database bersih tidak dapat selesai dengan graph saat ini.

Status **OPEN**. Continuation referensi yang memilih persis histori asli dan memastikan Director sudah ada dapat menghasilkan referensi historis yang benar, tetapi **bukan perbaikan dependency source**. Perbaikan harus menambahkan dependency yang tepat, memeriksa konsistensi/cycle graph, serta membuktikan target Marketing0008 dan Marketing0009 pada database baru di SQLite dan PostgreSQL. Bukti kegagalan asli dan review `.local/quality-20261009/marketing-ad-reference-dependency-review.md` tetap menjadi acuan batch berikutnya. Gate enam kasus MKT-03 tidak diperluas menjadi sertifikasi installability bersih atau kesiapan seluruh aplikasi.

## Batas dan pekerjaan berikutnya

[Daftar kebutuhan penuh](FULL-APP-STATUS-2026-10-09.md) dan [kontrak MKT-03](MARKETING-AD-RECOVERY-PLAN-2026-10-09.md) tetap menjadi acuan. Selain dependency migrasi, pekerjaan masih mencakup koreksi identitas marketing dari invoice keliru (MKT-02), KPI operasional tambahan (MKT-04), adapter impor platform (MKT-05), serta sisa Finance/Tax/Director/Operations/HR dan portal mitra. Penggantian satu total menjadi beberapa baris belum ditutup oleh kemampuan satu pengganti ini.

Kelengkapan transaksi/dokumen perusahaan, keputusan pajak profesional, UAT staf, koneksi live BNI/Meta/Google/WA/SEO/OpenRouter, cloud/domain/HTTPS dan pemulihan eksternal tetap belum disertifikasi. Rilis ini tidak membuat transaksi nyata untuk menutup sumber yang kurang, tidak mengirim laporan ke DJP, dan tidak menyatakan seluruh tujuan ERP selesai.
