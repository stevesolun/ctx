---
version: alpha
name: Social Dusk
description: Late-night feed: indigo dusk, lilac strokes, amber taps.
colors:
  primary: "#ECE9FF"
  secondary: "#837CB0"
  tertiary: "#FFB257"
  neutral: "#0E0B1B"
  surface: "#181336"
  on-primary: "#0E0B1B"
typography:
  display:
    fontFamily: Manrope
    fontSize: 3.75rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Manrope
    fontSize: 2rem
    fontWeight: 700
  body:
    fontFamily: Manrope
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Manrope
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.06em"
rounded:
  sm: 8px
  md: 14px
  lg: 22px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A social-app palette tuned for AMOLED screens at midnight.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#ECE9FF`):** Headlines and core text.
- **Secondary (`#837CB0`):** Borders, captions, and metadata.
- **Tertiary (`#FFB257`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0E0B1B`):** The page foundation.

## Typography

- **display:** Manrope 3.75rem
- **h1:** Manrope 2rem
- **body:** Manrope 0.95rem
- **label:** Manrope 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
