---
version: alpha
name: Y2K Chrome
description: Frutiger Aero meets the CD-ROM menu.
colors:
  primary: "#0F172A"
  secondary: "#60A5FA"
  tertiary: "#06B6D4"
  neutral: "#E0F2FE"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Syne
    fontSize: 4.5rem
    fontWeight: 800
    letterSpacing: "-0.04em"
  h1:
    fontFamily: Syne
    fontSize: 2.5rem
    fontWeight: 700
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.04em"
rounded:
  sm: 10px
  md: 18px
  lg: 28px
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

Glossy chrome gradients, bubble typography, cyan sheen. Built for nostalgia with a sharp contemporary edge.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0F172A`):** Headlines and core text.
- **Secondary (`#60A5FA`):** Borders, captions, and metadata.
- **Tertiary (`#06B6D4`):** The sole driver for interaction. Reserve it.
- **Neutral (`#E0F2FE`):** The page foundation.

## Typography

- **display:** Syne 4.5rem
- **h1:** Syne 2.5rem
- **body:** Inter 0.95rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
