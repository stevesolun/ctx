---
version: alpha
name: Portfolio Studio
description: Designer portfolio: off-white, ink, one deliberate accent.
colors:
  primary: "#131210"
  secondary: "#716E68"
  tertiary: "#E6552F"
  neutral: "#F3F0EA"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Fraunces
    fontSize: 5rem
    fontWeight: 500
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.5rem
    fontWeight: 500
  body:
    fontFamily: Inter
    fontSize: 0.98rem
    lineHeight: 1.65
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.06em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
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

A designer-portfolio system: off-white paper, ink text, one deliberate orange accent.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#131210`):** Headlines and core text.
- **Secondary (`#716E68`):** Borders, captions, and metadata.
- **Tertiary (`#E6552F`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F3F0EA`):** The page foundation.

## Typography

- **display:** Fraunces 5rem
- **h1:** Fraunces 2.5rem
- **body:** Inter 0.98rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
