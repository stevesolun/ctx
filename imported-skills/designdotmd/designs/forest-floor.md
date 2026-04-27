---
version: alpha
name: Forest Floor
description: Moss, bark, and a ribbon of copper.
colors:
  primary: "#1F3529"
  secondary: "#5C6B5E"
  tertiary: "#C9733A"
  neutral: "#E8E2D0"
  surface: "#F5F1E4"
  on-primary: "#F5F1E4"
typography:
  display:
    fontFamily: Cormorant Garamond
    fontSize: 4.5rem
    fontWeight: 500
    letterSpacing: "-0.015em"
  h1:
    fontFamily: Cormorant Garamond
    fontSize: 2.75rem
    fontWeight: 500
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: Inter
    fontSize: 0.75rem
    letterSpacing: "0.08em"
rounded:
  sm: 4px
  md: 8px
  lg: 16px
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

A grounded, outdoorsy palette. Deep forest primary, warm bark neutrals, copper accent. Feels like a well-bound field journal.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1F3529`):** Headlines and core text.
- **Secondary (`#5C6B5E`):** Borders, captions, and metadata.
- **Tertiary (`#C9733A`):** The sole driver for interaction. Reserve it.
- **Neutral (`#E8E2D0`):** The page foundation.

## Typography

- **display:** Cormorant Garamond 4.5rem
- **h1:** Cormorant Garamond 2.75rem
- **body:** Inter 1rem
- **label:** Inter 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
