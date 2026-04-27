---
version: alpha
name: Dating Blush
description: Dating app: blush cream, lipstick red, flirty serif.
colors:
  primary: "#2B1014"
  secondary: "#936668"
  tertiary: "#E84B55"
  neutral: "#FBEDE8"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: DM Serif Display
    fontSize: 4.5rem
    fontWeight: 400
    letterSpacing: "-0.015em"
  h1:
    fontFamily: DM Serif Display
    fontSize: 2.4rem
    fontWeight: 400
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.6
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.08em"
rounded:
  sm: 14px
  md: 22px
  lg: 34px
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

A dating-app palette: blush cream surface, lipstick red accent, flirty serif display.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2B1014`):** Headlines and core text.
- **Secondary (`#936668`):** Borders, captions, and metadata.
- **Tertiary (`#E84B55`):** The sole driver for interaction. Reserve it.
- **Neutral (`#FBEDE8`):** The page foundation.

## Typography

- **display:** DM Serif Display 4.5rem
- **h1:** DM Serif Display 2.4rem
- **body:** Inter 1rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
