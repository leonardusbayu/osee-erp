# OSEE website branding sources

Verified on 7 September 2026 from the public [One Stop English Education homepage](https://onestopenglisheducation.com/). The web reader timed out; the same official URL was successfully retrieved directly over HTTPS with Python urllib. Values below come from HTML/CSS declarations, not screenshot color estimates.

## Original assets

All downloaded files are byte-for-byte copies of their official source responses. They have not been generated, recolored, cropped, or re-encoded. PNG format, dimensions and transparency were checked with Pillow; the full logo and 192-pixel icon were visually inspected.

| Local asset | Official source and observed use | Dimensions |
| --- | --- | --- |
| `static/brand/osee-logo.png` | [LOGO-OSEEE.png](https://onestopenglisheducation.com/wp-content/uploads/2025/10/LOGO-OSEEE.png), homepage sticky-header image, WordPress attachment `wp-image-1991` | 812 x 186, transparent RGBA, ratio 4.36559:1 |
| `static/brand/osee-icon-192.png` | [cropped-LOGO-OSEE-192x192.png](https://onestopenglisheducation.com/wp-content/uploads/2025/07/cropped-LOGO-OSEE-192x192.png), homepage `rel="icon"` | 192 x 192, transparent RGBA |
| `static/brand/osee-icon-32.png` | [cropped-LOGO-OSEE-32x32.png](https://onestopenglisheducation.com/wp-content/uploads/2025/07/cropped-LOGO-OSEE-32x32.png), homepage `rel="icon"` | 32 x 32, transparent RGBA |

The full horizontal logo combines the red/black circular symbol with its black wordmark. Preserve its aspect ratio and place it on a light surface. The official header uses `width:160px;height:auto`. The square icon is the site's separate official favicon asset, not a crop made for this ERP.

SHA-256 checksums:

```text
osee-logo.png      5a9ae2029fa7365adfeed3b2d7318baddabfae6441be6339b8924b7a9ce5ddbd
osee-icon-192.png  51ca0821194da5026d2cd71610cfeef221a33c732d0adad8ed7f8b3b94252caf
osee-icon-32.png   74d75c1ebc1fcc1dd65d11a9dc7e309998e68fa6b43ba310900a4810c95c1db3
```

## Colors in the live homepage

Every declaration in this table was present in the [homepage HTML and its inline styles](https://onestopenglisheducation.com/).

| Role / source declaration | Exact value |
| --- | --- |
| Repeated registration and purchase CTA inline `background-color` | `#B51010` |
| Hero radial-gradient first stop, `rgb(198,6,6)` | `#C60606` |
| Hero radial-gradient last stop, `rgb(115,8,8)` | `#730808` |
| Sticky-header inline background | `#F8F8F8` |
| Body text / `--wp--preset--color--contrast` | `#222121` |
| `--wp--preset--color--contrast-2` | `#636363` |
| Repeated card inline border | `#EAECF0` |
| `--wp--preset--color--base` | `#F9F9F9` |
| `--wp--preset--color--base-2` | `#FFFFFF` |
| Explicit WordPress `--wp--preset--color--primary` | `#EB0E0E` |
| Explicit WordPress `--wp--preset--color--secondary` | `#707C00` |
| `--wp--preset--color--button-hover-color` | `#FF8800` |

The named WordPress presets and the actual inline CTA colors differ. Treat `#EB0E0E` / `#707C00` as the site's declared primary/secondary tokens; do not mislabel the olive secondary token as the current hero or CTA color. The repeated CTA and hero declarations support a red-led ERP palette. Using a flat red treatment in the app, preserving green/amber/red semantic statuses, and adding accessible lighter interaction surfaces are ERP design decisions, not claims that those additions are official brand rules. No separate brand standards manual was consulted.

## Applying the identity to future modules

Use the shared `:root` tokens in `static/app.css`: `--primary` for actions, `--primary-soft` for selection, `--brand-deep` for featured surfaces, and `--ink` / `--muted` / `--line` for neutral content. The hover shade and pale red tints are derived interface colors. Green success and amber review statuses keep their operational meanings.

New module pages extend `templates/base.html` to inherit the official sidebar logo, favicon and palette. Use `.brand-logo` with the local PNG, preserving its intrinsic proportions and a light background; do not recreate the old text-only mark. The login and invoice detail pages use the same asset. PDF decoration is centralized in `webapp/documents.py`.

Verified after the update: desktop and mobile login/dashboard layouts, official logo loading, the tax chat, invoice and report screens, and rendered invoice/monthly PDFs. All ten existing PDF integrity tests passed.
