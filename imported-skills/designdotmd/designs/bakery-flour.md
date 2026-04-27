---
version: alpha
name: Bakery Flour
description: Artisan bakery: flour dust, rye, sourdough crust.
colors:
  primary: "#2B1E12"
  secondary: "#9B8670"
  tertiary: "#C46A1E"
  neutral: "#F5EEDC"
  surface: "#FBF6E9"
  on-primary: "#FBF6E9"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 5rem
    fontWeight: 500
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.5rem
    fontWeight: 500
  body:
    fontFamily: Lora
    fontSize: 1.02rem
    lineHeight: 1.7
  label:
    fontFamily: Lora
    fontSize: 0.74rem
    letterSpacing: "0.12em"
rounded:
  sm: 8px
  md: 14px
  lg: 24px
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

An artisan-bakery palette: flour-white surfaces, rye browns, crusty ember accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2B1E12`):** Headlines and core text.
- **Secondary (`#9B8670`):** Borders, captions, and metadata.
- **Tertiary (`#C46A1E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F5EEDC`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 5rem
- **h1:** Cormorant Garamond 2.5rem
- **body:** Lora 1.02rem
- **label:** Lora 0.74rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
