---
version: alpha
name: Swiss Grid
description: Helvetica, a 12-column grid, and a single red.
colors:
  primary: "#000000"
  secondary: "#555555"
  tertiary: "#DC2626"
  neutral: "#F5F5F5"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Inter Tight
    fontSize: 4rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Inter Tight
    fontSize: 2.25rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Inter
    fontSize: 0.75rem
    letterSpacing: "0.02em"
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

Classical International Typographic Style: flat whites, blacks, a disciplined grid, and a decisive red signal.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#000000`):** Headlines and core text.
- **Secondary (`#555555`):** Borders, captions, and metadata.
- **Tertiary (`#DC2626`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F5F5F5`):** The page foundation.

## Typography

- **display:** Inter Tight 4rem
- **h1:** Inter Tight 2.25rem
- **body:** Inter 0.95rem
- **label:** Inter 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
