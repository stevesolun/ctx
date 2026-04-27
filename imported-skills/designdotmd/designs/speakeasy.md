---
version: alpha
name: Speakeasy
description: Hidden bar: velvet black, candle amber, brass knob.
colors:
  primary: "#EAD9B4"
  secondary: "#8E7C5C"
  tertiary: "#CB8B3A"
  neutral: "#0E0B08"
  surface: "#17120C"
  on-primary: "#EAD9B4"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: 5rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Playfair Display
    fontSize: 2.4rem
    fontWeight: 600
  body:
    fontFamily: Lora
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Lora
    fontSize: 0.74rem
    fontWeight: 600
    letterSpacing: "0.24em"
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

A speakeasy palette: velvet black, candle amber accent, brass detail highlights.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#EAD9B4`):** Headlines and core text.
- **Secondary (`#8E7C5C`):** Borders, captions, and metadata.
- **Tertiary (`#CB8B3A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0E0B08`):** The page foundation.

## Typography

- **display:** Playfair Display 5rem
- **h1:** Playfair Display 2.4rem
- **body:** Lora 1rem
- **label:** Lora 0.74rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
