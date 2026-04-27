---
version: alpha
name: Arctic
description: Glacier white, ice blue, breath of steam.
colors:
  primary: "#0E1A24"
  secondary: "#627A8B"
  tertiary: "#2563EB"
  neutral: "#EEF4F9"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Manrope
    fontSize: 3.75rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Manrope
    fontSize: 2.25rem
    fontWeight: 700
  body:
    fontFamily: Manrope
    fontSize: 1rem
    lineHeight: 1.6
  label:
    fontFamily: Manrope
    fontSize: 0.72rem
    letterSpacing: "0.06em"
rounded:
  sm: 6px
  md: 12px
  lg: 20px
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

Nordic clarity. Glacial whites, cool blue accent, generous negative space. For products that want to feel rigorous without being cold.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0E1A24`):** Headlines and core text.
- **Secondary (`#627A8B`):** Borders, captions, and metadata.
- **Tertiary (`#2563EB`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EEF4F9`):** The page foundation.

## Typography

- **display:** Manrope 3.75rem
- **h1:** Manrope 2.25rem
- **body:** Manrope 1rem
- **label:** Manrope 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
