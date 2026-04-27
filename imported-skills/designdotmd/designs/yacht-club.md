---
version: alpha
name: Yacht Club
description: Regatta: navy, rope cream, signal flag red.
colors:
  primary: "#F7F0DE"
  secondary: "#B4A88A"
  tertiary: "#C42C2C"
  neutral: "#0B2440"
  surface: "#142F54"
  on-primary: "#F7F0DE"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: 4.5rem
    fontWeight: 600
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Playfair Display
    fontSize: 2.3rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.18em"
rounded:
  sm: 2px
  md: 3px
  lg: 5px
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

A yacht-club brand palette: deep navy, cream sail, signal red.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F7F0DE`):** Headlines and core text.
- **Secondary (`#B4A88A`):** Borders, captions, and metadata.
- **Tertiary (`#C42C2C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0B2440`):** The page foundation.

## Typography

- **display:** Playfair Display 4.5rem
- **h1:** Playfair Display 2.3rem
- **body:** Inter 1rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
