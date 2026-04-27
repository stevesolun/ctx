---
version: alpha
name: Jazz Club
description: Blue Note vibe: ink blue, cream, trumpet gold.
colors:
  primary: "#F4ECD8"
  secondary: "#A89B7A"
  tertiary: "#E2B040"
  neutral: "#0F1F3D"
  surface: "#162748"
  on-primary: "#F4ECD8"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: 4.5rem
    fontWeight: 800
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Playfair Display
    fontSize: 2.4rem
    fontWeight: 700
  body:
    fontFamily: Libre Caslon Text
    fontSize: 1rem
    lineHeight: 1.7
  label:
    fontFamily: Bebas Neue
    fontSize: 0.85rem
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

A jazz-club palette inspired by mid-century record sleeves: ink blue, cream, gold.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F4ECD8`):** Headlines and core text.
- **Secondary (`#A89B7A`):** Borders, captions, and metadata.
- **Tertiary (`#E2B040`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0F1F3D`):** The page foundation.

## Typography

- **display:** Playfair Display 4.5rem
- **h1:** Playfair Display 2.4rem
- **body:** Libre Caslon Text 1rem
- **label:** Bebas Neue 0.85rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
