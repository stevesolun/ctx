---
version: alpha
name: Rave Poster
description: Warehouse flyer: acid yellow, smudge ink, 4am energy.
colors:
  primary: "#FAFF00"
  secondary: "#8A8A00"
  tertiary: "#FF007A"
  neutral: "#0A0A0A"
  surface: "#141414"
  on-primary: "#0A0A0A"
typography:
  display:
    fontFamily: Archivo Black
    fontSize: 5.5rem
    fontWeight: 900
    letterSpacing: "-0.04em"
  h1:
    fontFamily: Archivo Black
    fontSize: 2.6rem
    fontWeight: 900
  body:
    fontFamily: Inter
    fontSize: 0.92rem
    lineHeight: 1.45
  label:
    fontFamily: Archivo Black
    fontSize: 0.72rem
    letterSpacing: "0.2em"
rounded:
  sm: 0px
  md: 0px
  lg: 0px
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

A nightlife-poster system that crackles.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#FAFF00`):** Headlines and core text.
- **Secondary (`#8A8A00`):** Borders, captions, and metadata.
- **Tertiary (`#FF007A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0A0A0A`):** The page foundation.

## Typography

- **display:** Archivo Black 5.5rem
- **h1:** Archivo Black 2.6rem
- **body:** Inter 0.92rem
- **label:** Archivo Black 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
