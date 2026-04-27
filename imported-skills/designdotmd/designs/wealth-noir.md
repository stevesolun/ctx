---
version: alpha
name: Wealth Noir
description: Private-banking black, champagne letterforms.
colors:
  primary: "#E9E3D4"
  secondary: "#8C8575"
  tertiary: "#C9A15A"
  neutral: "#0A0A09"
  surface: "#131211"
  on-primary: "#0A0A09"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: 4.5rem
    fontWeight: 500
  h1:
    fontFamily: Playfair Display
    fontSize: 2.5rem
    fontWeight: 500
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.65
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.16em"
rounded:
  sm: 1px
  md: 2px
  lg: 3px
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

A discreet, high-net-worth system.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E9E3D4`):** Headlines and core text.
- **Secondary (`#8C8575`):** Borders, captions, and metadata.
- **Tertiary (`#C9A15A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0A0A09`):** The page foundation.

## Typography

- **display:** Playfair Display 4.5rem
- **h1:** Playfair Display 2.5rem
- **body:** Inter 0.95rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
