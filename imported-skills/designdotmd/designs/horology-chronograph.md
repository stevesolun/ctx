---
version: alpha
name: Horology Chronograph
description: Swiss watchmaking: dial black, subdial silver, second red.
colors:
  primary: "#E8E6DF"
  secondary: "#8C8A82"
  tertiary: "#C42A2A"
  neutral: "#0C0D10"
  surface: "#151619"
  on-primary: "#E8E6DF"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 4.5rem
    fontWeight: 400
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.4rem
    fontWeight: 400
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.65
  label:
    fontFamily: Inter
    fontSize: 0.7rem
    fontWeight: 500
    letterSpacing: "0.28em"
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

A watchmaker's palette: dial-black surface, subdial silver, single red second-hand accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#E8E6DF`):** Headlines and core text.
- **Secondary (`#8C8A82`):** Borders, captions, and metadata.
- **Tertiary (`#C42A2A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#0C0D10`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 4.5rem
- **h1:** Cormorant Garamond 2.4rem
- **body:** Inter 0.95rem
- **label:** Inter 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
