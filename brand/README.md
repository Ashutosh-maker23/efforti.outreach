# Efforti brand assets

The logo used across the app UI. Both SVGs are **background-free** and fill with
`currentColor`, so they inherit whatever colour you set on them.

| File | What it is |
| --- | --- |
| `efforti-lockup.svg` | Mark + "efforti" wordmark. viewBox `0 0 129.92 29.91`. Used in the sidebar, the landing nav and the footer. |
| `efforti-mark.svg` | The mark alone, square. viewBox `0 0 29.93 29.91`. Used for the favicon and the collapsed sidebar rail. |
| `efforti-logo-source.png` | The original raster these were traced from — recovered from the mailbox signature stored in `outreach.db`. Kept only as provenance. |

## Where they live in the app

The SVGs are **inlined** into `app/ui/base.html` and `app/ui/landing.html` rather
than linked, because the app serves no static directory. If you edit a file here,
re-inline it in both templates.

## Colour

```
--brand: #0F9CD8   /* the logo colour — logo only */
--accent          /* interactive fill; a darker step of the same hue */
```

The raw brand cyan only reaches **3.1:1** against white, which is fine for a logo
(WCAG exempts brand marks) but fails for button and link text. So `--accent` is a
darker step of the same hue (`#0A76A8` light / `#42B8EA` dark) and every filled
accent surface pairs it with `--on-accent` for its text.

## Provenance

The original PNG had a solid white background and no alpha. These SVGs were
produced by tracing its anti-aliased edges — measuring ink as *distance from
white* (not darkness, which collapses on a mid-luminance cyan), extracting the
0.5 iso-line with marching squares, then simplifying. The result is a true
vectorisation of the supplied artwork, not a redraw.

If you have the original vector from your designer, drop it in and re-inline —
it will always beat a trace.
