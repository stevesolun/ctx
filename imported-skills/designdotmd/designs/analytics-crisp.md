---
version: alpha
name: Analytics Crisp
description: Analytics: crisp white, chart iris, chart peach.
colors:
  primary: "#121418"
  secondary: "#6A6F77"
  tertiary: "#5A4FE0"
  neutral: "#F7F8FA"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Inter
    fontSize: 3.5rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Inter
    fontSize: 1.9rem
    fontWeight: 700
  body:
    fontFamily: Inter
    fontSize: 0.92rem
    lineHeight: 1.55
  label:
    fontFamily: Inter
    fontSize: 0.7rem
    fontWeight: 600
    letterSpacing: "0.04em"
rounded:
  sm: 4px
  md: 8px
  lg: 14px
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

An analytics-dashboard palette: paper-white surfaces, chart-iris primary, chart-peach secondary.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#121418`):** Headlines and core text.
- **Secondary (`#6A6F77`):** Borders, captions, and metadata.
- **Tertiary (`#5A4FE0`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F7F8FA`):** The page foundation.

## Typography

- **display:** Inter 3.5rem
- **h1:** Inter 1.9rem
- **body:** Inter 0.92rem
- **label:** Inter 0.7rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
