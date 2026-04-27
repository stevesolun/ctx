---
version: alpha
name: Heritage
description: Architectural minimalism meets journalistic gravitas.
colors:
  primary: "#1A1C1E"
  secondary: "#6C7278"
  tertiary: "#B8422E"
  neutral: "#F7F5F2"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Fraunces
    fontSize: 4rem
    fontWeight: 500
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.5rem
    fontWeight: 500
  body:
    fontFamily: Public Sans
    fontSize: 1rem
    lineHeight: 1.6
  label:
    fontFamily: Space Grotesk
    fontSize: 0.75rem
    letterSpacing: "0.08em"
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

A warm, high-contrast palette rooted in broadsheet newspapers and matte galleries. Deep ink on warm limestone, one single accent for action.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1A1C1E`):** Headlines and core text.
- **Secondary (`#6C7278`):** Borders, captions, and metadata.
- **Tertiary (`#B8422E`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F7F5F2`):** The page foundation.

## Typography

- **display:** Fraunces 4rem
- **h1:** Fraunces 2.5rem
- **body:** Public Sans 1rem
- **label:** Space Grotesk 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
