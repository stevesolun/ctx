---
version: alpha
name: Ski Alpine
description: Alpine signage: glacier blue, piste red, powder white.
colors:
  primary: "#0F2436"
  secondary: "#5E7386"
  tertiary: "#E63946"
  neutral: "#F0F5FA"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Oswald
    fontSize: 4.5rem
    fontWeight: 700
    letterSpacing: "0.02em"
  h1:
    fontFamily: Oswald
    fontSize: 2.3rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Oswald
    fontSize: 0.78rem
    letterSpacing: "0.14em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
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

A ski-resort palette: glacier blue primary, piste-red accent, powder white surface.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0F2436`):** Headlines and core text.
- **Secondary (`#5E7386`):** Borders, captions, and metadata.
- **Tertiary (`#E63946`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F0F5FA`):** The page foundation.

## Typography

- **display:** Oswald 4.5rem
- **h1:** Oswald 2.3rem
- **body:** Inter 0.95rem
- **label:** Oswald 0.78rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
