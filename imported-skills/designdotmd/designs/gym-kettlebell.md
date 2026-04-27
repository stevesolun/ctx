---
version: alpha
name: Gym Kettlebell
description: Barbell black: chalk white, rep red, rubber mat.
colors:
  primary: "#F5F5F3"
  secondary: "#898685"
  tertiary: "#E63946"
  neutral: "#0A0A0A"
  surface: "#141414"
  on-primary: "#0A0A0A"
typography:
  display:
    fontFamily: Archivo Black
    fontSize: 4.5rem
    fontWeight: 900
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Archivo Black
    fontSize: 2.4rem
    fontWeight: 900
  body:
    fontFamily: Archivo
    fontSize: 0.95rem
    lineHeight: 1.5
  label:
    fontFamily: Archivo Black
    fontSize: 0.72rem
    letterSpacing: "0.14em"
rounded:
  sm: 2px
  md: 4px
  lg: 6px
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

A gym-app palette: chalk white, barbell black, rep-red counter accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F5F5F3`):** Headlines and core text.
- **Secondary (`#898685`):** Borders, captions, and metadata.
- **Tertiary (`#E63946`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0A0A0A`):** The page foundation.

## Typography

- **display:** Archivo Black 4.5rem
- **h1:** Archivo Black 2.4rem
- **body:** Archivo 0.95rem
- **label:** Archivo Black 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
